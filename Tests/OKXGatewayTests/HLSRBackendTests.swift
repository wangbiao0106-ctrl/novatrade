import Foundation
import Testing
import ATKGateway
import TradingDomain
@testable import TradingService

/// Authenticated OKX demo-account double used by the HLSR backend tests.  It
/// deliberately keeps the remote position separate from the local paper
/// ledger: HLSR may only advance a leg after this snapshot reports the fill.
private actor HLSRBackendRunner: ATKCommandRunning {
    let instrumentID = "ALT-USDT-SWAP"
    let otherInstrumentID = "OTHER-USDT-SWAP"
    private var positions: [PositionSnapshot]
    private var remoteOrders: [OrderSnapshot] = []
    private var clientOrders: [String: OrderSnapshot] = [:]
    private var rejectNextPlace: Bool
    private var nextOrderNumber = 1
    private var placeCommands: [[String]] = []
    /// Simulates a lost submit response and a failing clOrdId lookup.
    private var placeResponseLost = false
    private var clientOrderLookupFails = false

    init(positions: [PositionSnapshot] = [], rejectNextPlace: Bool = false) {
        self.positions = positions
        self.rejectNextPlace = rejectNextPlace
    }

    func setPositions(_ value: [PositionSnapshot]) { positions = value }
    func setRemoteOrders(_ value: [OrderSnapshot]) { remoteOrders = value }
    func setClientOrder(_ clientOrderID: String, order: OrderSnapshot) { clientOrders[clientOrderID] = order }
    func setRejectNextPlace(_ value: Bool) { rejectNextPlace = value }
    func setPlaceResponseLost(_ value: Bool) { placeResponseLost = value }
    func setClientOrderLookupFails(_ value: Bool) { clientOrderLookupFails = value }
    func commandsContainingPlace() -> [[String]] { placeCommands }

    func run(arguments: [String]) async throws -> ATKCommandResult {
        let command = arguments.joined(separator: " ")
        if command.contains("swap place") {
            placeCommands.append(arguments)
            if placeResponseLost { throw ATKError.unavailable("ATK 命令超时") }
            if rejectNextPlace {
                rejectNextPlace = false
                return ATKCommandResult(stdout: #"{"code":"51000","msg":"rejected"}"#)
            }
            let orderID = "hlsr-order-\(nextOrderNumber)"
            nextOrderNumber += 1
            return ATKCommandResult(stdout: #"{"data":[{"ordId":"ORDER_ID"}]}"#.replacingOccurrences(of: "ORDER_ID", with: orderID))
        }
        if command == "config show --json" {
            return ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#)
        }
        if command.hasPrefix("account balance-all") {
            return ATKCommandResult(stdout: #"{"trading":{"totalEq":"100000","adjEq":"100000","details":[{"ccy":"USDT","eq":"100000","availEq":"100000","eqUsd":"100000"}]},"valuation":{"totalBal":"100000"}}"#)
        }
        if command == "account config --json" { return ATKCommandResult(stdout: #"[{"label":"demo"}]"#) }
        if command.hasPrefix("account positions") || command == "swap positions --json" {
            return ATKCommandResult(stdout: encodePositions())
        }
        if command.hasPrefix("account bills") { return ATKCommandResult(stdout: "[]") }
        if command == "swap orders --json" {
            return ATKCommandResult(stdout: encodeOrders())
        }
        if command.contains("swap get ") {
            if clientOrderLookupFails { throw ATKError.unavailable("ATK 查询超时") }
            if let index = arguments.firstIndex(of: "--clOrdId"), index + 1 < arguments.count,
               let order = clientOrders[arguments[index + 1]] {
                return ATKCommandResult(stdout: "[{\"instId\":\"\(order.instrumentID)\",\"ordId\":\"\(order.id)\",\"clOrdId\":\"\(arguments[index + 1])\",\"side\":\"buy\",\"state\":\"\(order.status)\",\"sz\":\"\(order.quantity)\",\"cTime\":\"1700000000000\"}]")
            }
            return ATKCommandResult(stdout: #"{"code":"51603","msg":"Order does not exist"}"#)
        }
        if command.hasPrefix("market ticker ") {
            let id = command.split(separator: " ").dropFirst(2).first.map(String.init) ?? instrumentID
            let last = id == otherInstrumentID ? "120" : "97"
            return ATKCommandResult(stdout: "[{\"instId\":\"\(id)\",\"last\":\"\(last)\",\"ts\":\"1700000000000\"}]")
        }
        if command.hasPrefix("market tickers SWAP") {
            // ALT is an eligible hot altcoin: it is non-mainstream, has a
            // positive 24h move, and exceeds the strategy's liquidity gate.
            return ATKCommandResult(stdout: #"[{"instId":"ALT-USDT-SWAP","last":"97","open24h":"60","volCcy24h":"40000000"},{"instId":"OTHER-USDT-SWAP","last":"120","open24h":"100","volCcy24h":"39000000"}]"#)
        }
        if command.hasPrefix("market instruments --instType SWAP") {
            let id = command.contains(otherInstrumentID) ? otherInstrumentID : instrumentID
            return ATKCommandResult(stdout: "[{\"instId\":\"\(id)\",\"ctVal\":\"1\",\"ctMult\":\"1\",\"lotSz\":\"1\",\"minSz\":\"1\",\"tickSz\":\"0.1\",\"state\":\"live\",\"ctType\":\"linear\",\"settleCcy\":\"USDT\"}]")
        }
        return ATKCommandResult(stdout: "[]")
    }

    private func encodePositions() -> String {
        let rows = positions.map { position in
            var row = "\"instId\":\"\(position.instrumentID)\",\"pos\":\"\(position.quantity)\",\"posId\":\"\(position.id)\",\"avgPx\":\"\(position.entryPrice)\",\"posSide\":\"\(position.side)\""
            if let mark = position.markPrice { row += ",\"markPx\":\"\(mark)\"" }
            return "{\(row)}"
        }.joined(separator: ",")
        return "[\(rows)]"
    }

    private func encodeOrders() -> String {
        let rows = remoteOrders.map { order in
            "{\"instId\":\"\(order.instrumentID)\",\"ordId\":\"\(order.id)\",\"side\":\"\(order.side)\",\"state\":\"\(order.status)\",\"sz\":\"\(order.quantity)\",\"cTime\":\"1700000000000\"}"
        }.joined(separator: ",")
        return "[\(rows)]"
    }
}

private let hlsrBackendSpec = SwapInstrumentSpec(
    instrumentID: "ALT-USDT-SWAP", ctVal: 1, lotSize: 1, minSize: 1,
    tickSize: 0.1, state: "live", ctType: "linear", settleCurrency: "USDT"
)

private func backendSignal(strategyID: UUID, price: Decimal = 100) -> StrategySignal {
    StrategySignal(
        strategyID: strategyID, type: "entry_short", price: price,
        reason: "HLSR integration fixture", timestamp: Date(timeIntervalSince1970: 1_700_000_000),
        stopPrice: 110, takePrice: 95, takePrices: [95, 90, 85],
        targetFractions: [0.3, 0.3, 0.4], moveStopToEntryAfterTP1: true,
        trailBars: 2, invalidationPrice: 108
    )
}

private func backendCandle(_ timestamp: TimeInterval, open: Decimal, high: Decimal, low: Decimal, close: Decimal, confirmed: Bool = true) -> Candle {
    Candle(timestamp: Date(timeIntervalSince1970: timestamp), open: open, high: high, low: low,
           close: close, volume: 1, quoteVolume: 500_000, confirmed: confirmed)
}

private func hlsrBackendConfig(id: UUID = UUID(), enabled: Bool = false) -> StrategyConfig {
    StrategyConfig(id: id, name: "高位扫顶反转做空", scope: .dynamic(.hotAltcoins),
                   interval: .fifteenMinutes, type: .hlsr, enabled: enabled,
                   riskPercent: 0.5, cooldownBars: 16)
}

private func backendDirectory() -> URL {
    FileManager.default.temporaryDirectory.appendingPathComponent("hlsr-backend-\(UUID().uuidString)", isDirectory: true)
}

private func makeBackend(runner: HLSRBackendRunner, directory: URL) -> TradingBackend {
    let market = MarketDataService(
        client: ATKClient(runner: runner),
        ttl: MarketCacheTTL(ticker: 0, contracts: 0, account: 0, positions: 0, orders: 0)
    )
    return TradingBackend(
        market: market,
        paper: PaperTradingStore(directory: directory),
        riskEngine: RiskEngine(limits: RiskLimits(maxMarginPercent: 100), initialEquity: 100_000)
    )
}

private struct HLSRBackendRuntimeFixture: Codable {
    let manager: HLSRPositionManager
    let remoteOrderID: String?
    let clientOrderID: String?
    let submittedAt: Date?
    let lastExitPrice: Decimal?
    let realizedExitPnL: Decimal?
}

private struct HLSRBackendStateFixture: Codable {
    let schemaVersion: Int
    let states: [String: HLSRBackendRuntimeFixture]
}

private struct HLSRBackendPendingExitFixture: Decodable {
    let positionID: String?
    let instrumentID: String
    let realizedPnL: Decimal?
}

private func persistHLSRRuntime(manager: HLSRPositionManager, positionID: String, instrumentID: String, directory: URL, remoteOrderID: String? = nil, clientOrderID: String? = nil, submittedAt: Date? = nil, lastExitPrice: Decimal? = nil) throws {
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let state = HLSRBackendRuntimeFixture(manager: manager, remoteOrderID: remoteOrderID, clientOrderID: clientOrderID, submittedAt: submittedAt, lastExitPrice: lastExitPrice, realizedExitPnL: nil)
    let file = HLSRBackendStateFixture(schemaVersion: 1, states: ["\(positionID):\(instrumentID)": state])
    try JSONEncoder().encode(file).write(to: directory.appendingPathComponent("hlsr-exit-state.json"))
}

private func recordHLSREntry(_ paper: PaperTradingStore, strategyID: UUID, signal: StrategySignal, quantity: Decimal = 10, requestedAt: Date = Date(timeIntervalSince1970: 1_700_000_000)) async {
    await paper.record(PaperOrder(strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "short", quantity: quantity, requestedAt: requestedAt, fillPrice: signal.price, status: "submitted", remoteOrderID: "entry-order", signal: signal))
}

@Test("HLSR entry submits on demo without attaching a single native TP")
func hlsrBackendEntryUsesThreeLegExitPlan() async throws {
    let runner = HLSRBackendRunner()
    let directory = backendDirectory()
    let backend = makeBackend(runner: runner, directory: directory)
    let config = try await backend.createStrategy(hlsrBackendConfig(enabled: true))
    _ = try await backend.startStrategy(config.id)
    _ = try await backend.contracts(forceRefresh: true)

    // The backend's scanner receives the 4H warm-up through the same paper
    // prewarm path as the production REST warm-up, then consumes realtime 15m
    // bars.  The final unconfirmed bar is the causal next-open entry.
    let fixtures = hlsrBackendFixtures()
    await backend.paper.prewarm(MarketSnapshot(instrumentID: "ALT-USDT-SWAP", interval: .fourHours, candles: fixtures.fourHour))
    for candle in fixtures.lower {
        _ = await backend.ingestRealtimeCandle(candle, instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    }

    let places = await runner.commandsContainingPlace()
    #expect(places.count == 1)
    let entry = try #require(places.first)
    #expect(entry.contains("--demo"))
    #expect(entry.contains("--side") && entry.contains("sell"))
    #expect(entry.contains("--slTriggerPx"))
    #expect(!entry.contains("--tpTriggerPx"))
}

@Test("HLSR exits ignore a candle belonging to another instrument")
func hlsrBackendExitUsesCandleInstrumentIdentity() async throws {
    let runner = HLSRBackendRunner(positions: [PositionSnapshot(id: "position-1", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 10, entryPrice: 100, markPrice: nil)])
    let directory = backendDirectory()
    let backend = makeBackend(runner: runner, directory: directory)
    let config = try await backend.createStrategy(hlsrBackendConfig())
    let signal = backendSignal(strategyID: config.id)
    await recordHLSREntry(backend.paper, strategyID: config.id, signal: signal)
    let manager = try HLSRPositionManager(strategyID: config.id, instrumentID: "ALT-USDT-SWAP", signal: signal, spec: hlsrBackendSpec, requestedQuantity: 10)
    try persistHLSRRuntime(manager: manager, positionID: "position-1", instrumentID: "ALT-USDT-SWAP", directory: directory)

    // OTHER's high/close would hit an ALT stop if the realtime bar were used
    // for every remote position.  Its identity must be checked first.
    _ = await backend.ingestRealtimeCandle(backendCandle(1_700_000_900, open: 120, high: 130, low: 119, close: 125), instrumentID: "OTHER-USDT-SWAP", interval: .fifteenMinutes)
    #expect((await runner.commandsContainingPlace()).isEmpty)
    _ = manager // keep the fixture's manager construction explicit for Codable coverage
}

@Test("HLSR TP1 waits for full quantity confirmation after a partial fill")
func hlsrBackendTP1RequiresFullFill() async throws {
    let runner = HLSRBackendRunner(positions: [PositionSnapshot(id: "position-1", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 10, entryPrice: 100, markPrice: 100)])
    let directory = backendDirectory()
    let backend = makeBackend(runner: runner, directory: directory)
    let config = try await backend.createStrategy(hlsrBackendConfig())
    let signal = backendSignal(strategyID: config.id)
    await recordHLSREntry(backend.paper, strategyID: config.id, signal: signal)

    let trigger = backendCandle(1_700_000_900, open: 100, high: 101, low: 94, close: 96)
    _ = await backend.ingestRealtimeCandle(trigger, instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    #expect((await runner.commandsContainingPlace()).count == 1)

    await runner.setRemoteOrders([OrderSnapshot(id: "hlsr-order-1", instrumentID: "ALT-USDT-SWAP", side: "buy", status: "live", quantity: 3, createdAt: .now)])
    await runner.setPositions([PositionSnapshot(id: "position-1", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 8, entryPrice: 100, markPrice: 96)])
    _ = await backend.ingestRealtimeCandle(backendCandle(1_700_001_800, open: 96, high: 97, low: 94, close: 95), instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    #expect((await runner.commandsContainingPlace()).count == 1) // partial fill does not advance TP1

    await runner.setPositions([PositionSnapshot(id: "position-1", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 7, entryPrice: 100, markPrice: 95)])
    _ = await backend.ingestRealtimeCandle(backendCandle(1_700_002_700, open: 95, high: 96, low: 93, close: 94), instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    #expect((await runner.commandsContainingPlace()).count == 1)
    let stateData = try Data(contentsOf: directory.appendingPathComponent("hlsr-exit-state.json"))
    let state = try JSONDecoder().decode(HLSRBackendStateFixture.self, from: stateData)
    #expect(state.states["position-1:ALT-USDT-SWAP"]?.manager.nextTargetIndex == 1)
    #expect(state.states["position-1:ALT-USDT-SWAP"]?.manager.activeStop == 100)
}

@Test("HLSR rejected reduce-only exits are retryable")
func hlsrBackendRejectRetriesSameLeg() async throws {
    let runner = HLSRBackendRunner(positions: [PositionSnapshot(id: "position-1", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 10, entryPrice: 100, markPrice: 100)], rejectNextPlace: true)
    let directory = backendDirectory()
    let backend = makeBackend(runner: runner, directory: directory)
    let config = try await backend.createStrategy(hlsrBackendConfig())
    let signal = backendSignal(strategyID: config.id)
    await recordHLSREntry(backend.paper, strategyID: config.id, signal: signal)
    let trigger = backendCandle(1_700_000_900, open: 100, high: 101, low: 94, close: 96)
    _ = await backend.ingestRealtimeCandle(trigger, instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    #expect((await runner.commandsContainingPlace()).count == 1)

    _ = await backend.ingestRealtimeCandle(backendCandle(1_700_001_800, open: 96, high: 97, low: 94, close: 95), instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    let places = await runner.commandsContainingPlace()
    #expect(places.count == 2)
    func clientID(_ command: [String]) -> String? {
        guard let index = command.firstIndex(of: "--clOrdId"), index + 1 < command.count else { return nil }
        return command[index + 1]
    }
    let firstClientID = clientID(places[0])
    let secondClientID = clientID(places[1])
    #expect(firstClientID != nil)
    #expect(firstClientID == secondClientID)
}

@Test("HLSR restart restores TP1 breakeven protection")
func hlsrBackendRestartRestoresBreakevenManager() async throws {
    let runner = HLSRBackendRunner(positions: [PositionSnapshot(id: "position-1", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 7, entryPrice: 100, markPrice: 101)])
    let directory = backendDirectory()
    let first = makeBackend(runner: runner, directory: directory)
    let config = try await first.createStrategy(hlsrBackendConfig())
    let signal = backendSignal(strategyID: config.id)
    await recordHLSREntry(first.paper, strategyID: config.id, signal: signal)
    var manager = try HLSRPositionManager(strategyID: config.id, instrumentID: "ALT-USDT-SWAP", signal: signal, spec: hlsrBackendSpec, requestedQuantity: 10)
    let maybeIntent = manager.evaluate(candle: backendCandle(1_700_000_900, open: 100, high: 101, low: 94, close: 96), position: PositionSnapshot(id: "position-1", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 10, entryPrice: 100, markPrice: 100))
    let intent = try #require(maybeIntent)
    let maybeLeg = manager.claim(intent)
    let leg = try #require(maybeLeg)
    let confirmed = manager.confirm(leg.id, position: PositionSnapshot(id: "position-1", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 7, entryPrice: 100, markPrice: 96))
    #expect(confirmed)
    #expect(manager.activeStop == 100)
    try persistHLSRRuntime(manager: manager, positionID: "position-1", instrumentID: "ALT-USDT-SWAP", directory: directory, lastExitPrice: 95)

    // Recreate both stores as the service does after a process restart. The
    // persisted strategy is paused for safety, but a live HLSR position still
    // owns its persisted manager and must retain breakeven protection.
    let restarted = makeBackend(runner: runner, directory: directory)
    _ = await restarted.ingestRealtimeCandle(backendCandle(1_700_003_600, open: 101, high: 102, low: 100.5, close: 101), instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    let places = await runner.commandsContainingPlace()
    #expect(places.count == 1)
    if let first = places.first {
        #expect(first.contains("--reduceOnly"))
    }
}

@Test("HLSR final exit settles its original position after restart when the instrument reopens")
func hlsrBackendFinalExitUsesOriginalPositionIdentityAfterRestart() async throws {
    let originalPosition = PositionSnapshot(id: "original-position", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 10, entryPrice: 100, markPrice: 110)
    let runner = HLSRBackendRunner(positions: [originalPosition])
    let directory = backendDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let first = makeBackend(runner: runner, directory: directory)
    let config = try await first.createStrategy(hlsrBackendConfig())
    let signal = backendSignal(strategyID: config.id)
    await recordHLSREntry(first.paper, strategyID: config.id, signal: signal)
    let decision = await first.riskEngine.authorize(instrumentID: "ALT-USDT-SWAP", notional: 1_000, margin: 1_000, strategyID: config.id, riskAmount: 100)
    #expect(decision.allowed)

    _ = await first.ingestRealtimeCandle(backendCandle(1_700_000_900, open: 110, high: 111, low: 109, close: 110), instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    #expect((await runner.commandsContainingPlace()).count == 1)
    let pendingData = try Data(contentsOf: directory.appendingPathComponent("pending-remote-exits.json"))
    let pending = try JSONDecoder().decode([String: HLSRBackendPendingExitFixture].self, from: pendingData)
    #expect(pending["original-position:ALT-USDT-SWAP"]?.positionID == "original-position")
    #expect((await first.riskEngine.snapshot()).strategyCapitals.first?.reservedCapital == 1_000)

    // OKX assigns a new posId when the instrument reopens. The old exit must
    // release its reservation even though this new position is nonzero.
    await runner.setPositions([PositionSnapshot(id: "new-position", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 10, entryPrice: 100, markPrice: 100)])
    let restarted = makeBackend(runner: runner, directory: directory)
    _ = await restarted.ingestRealtimeCandle(backendCandle(1_700_001_800, open: 100, high: 101, low: 99, close: 100), instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)

    let snapshot = await restarted.riskEngine.snapshot()
    let capital = try #require(snapshot.strategyCapitals.first)
    #expect(capital.reservedCapital == 0)
    #expect(capital.openRisk == 0)
    #expect(capital.openPositions == 0)
    #expect(capital.rolloverCount == 1)
    let settledData = try Data(contentsOf: directory.appendingPathComponent("pending-remote-exits.json"))
    #expect(try JSONDecoder().decode([String: HLSRBackendPendingExitFixture].self, from: settledData).isEmpty)
}

@Test("HLSR restart resolves persisted leg by client order ID without resubmitting")
func hlsrBackendPendingLegRecoversAcceptedOrderByClientID() async throws {
    let position = PositionSnapshot(id: "position-1", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 10, entryPrice: 100, markPrice: 100)
    let runner = HLSRBackendRunner(positions: [position])
    let directory = backendDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let first = makeBackend(runner: runner, directory: directory)
    let config = try await first.createStrategy(hlsrBackendConfig())
    let signal = backendSignal(strategyID: config.id)
    await recordHLSREntry(first.paper, strategyID: config.id, signal: signal)
    var manager = try HLSRPositionManager(strategyID: config.id, instrumentID: "ALT-USDT-SWAP", signal: signal, spec: hlsrBackendSpec, requestedQuantity: 10)
    guard let intent = manager.evaluate(
        candle: backendCandle(1_700_000_900, open: 100, high: 101, low: 94, close: 96),
        position: position
    ) else {
        Issue.record("expected HLSR exit intent")
        return
    }
    guard let leg = manager.claim(intent) else {
        Issue.record("expected HLSR exit leg")
        return
    }
    let clientOrderID = leg.id.uuidString.replacingOccurrences(of: "-", with: "")
    try persistHLSRRuntime(manager: manager, positionID: position.id, instrumentID: position.instrumentID, directory: directory, clientOrderID: clientOrderID, submittedAt: .now, lastExitPrice: leg.price)
    await runner.setClientOrder(clientOrderID, order: OrderSnapshot(id: "accepted-after-crash", instrumentID: position.instrumentID, side: "buy", status: "live", quantity: leg.quantity))

    let restarted = makeBackend(runner: runner, directory: directory)
    _ = await restarted.ingestRealtimeCandle(backendCandle(1_700_001_800, open: 100, high: 101, low: 99, close: 100), instrumentID: position.instrumentID, interval: .fifteenMinutes)
    #expect((await runner.commandsContainingPlace()).isEmpty)
    let stateData = try Data(contentsOf: directory.appendingPathComponent("hlsr-exit-state.json"))
    let state = try JSONDecoder().decode(HLSRBackendStateFixture.self, from: stateData)
    #expect(state.states["position-1:ALT-USDT-SWAP"]?.remoteOrderID == "accepted-after-crash")
}

@Test("Ordinary strategy exit restores its pending submission latch after restart")
func ordinaryStrategyPendingExitDoesNotResubmitAfterRestart() async throws {
    let position = PositionSnapshot(id: "ordinary-position", instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 10, entryPrice: 100, markPrice: 110)
    let runner = HLSRBackendRunner(positions: [position])
    let directory = backendDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let first = makeBackend(runner: runner, directory: directory)
    let config = try await first.createStrategy(StrategyConfig(name: "二次扫顶", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort, enabled: false, riskPercent: 0.5))
    await recordHLSREntry(first.paper, strategyID: config.id, signal: backendSignal(strategyID: config.id))
    let decision = await first.riskEngine.authorize(instrumentID: "ALT-USDT-SWAP", notional: 1_000, margin: 1_000, strategyID: config.id, riskAmount: 100)
    #expect(decision.allowed)

    _ = await first.ingestRealtimeCandle(backendCandle(1_700_000_900, open: 110, high: 111, low: 109, close: 110), instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    #expect((await runner.commandsContainingPlace()).count == 1)
    await runner.setRemoteOrders([OrderSnapshot(id: "hlsr-order-1", instrumentID: "ALT-USDT-SWAP", side: "buy", status: "live", quantity: 10, createdAt: .now)])

    let restarted = makeBackend(runner: runner, directory: directory)
    _ = await restarted.ingestRealtimeCandle(backendCandle(1_700_001_800, open: 110, high: 111, low: 109, close: 110), instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    #expect((await runner.commandsContainingPlace()).count == 1)
    #expect((await restarted.riskEngine.snapshot()).strategyCapitals.first?.reservedCapital == 1_000)
    let data = try Data(contentsOf: directory.appendingPathComponent("pending-remote-exits.json"))
    let pending = try JSONDecoder().decode([String: HLSRBackendPendingExitFixture].self, from: data)
    #expect(pending["ordinary-position:ALT-USDT-SWAP"]?.positionID == "ordinary-position")
}

/// Drives the HLSR entry fixture with a lost submit response and a failing
/// clOrdId lookup, returning the backend, strategy and entry clOrdId.
private func submitHLSREntryWithUnknownOutcome(runner: HLSRBackendRunner, directory: URL) async throws -> (TradingBackend, StrategyConfig, String) {
    await runner.setPlaceResponseLost(true)
    await runner.setClientOrderLookupFails(true)
    let backend = makeBackend(runner: runner, directory: directory)
    let config = try await backend.createStrategy(hlsrBackendConfig(enabled: true))
    _ = try await backend.startStrategy(config.id)
    _ = try await backend.contracts(forceRefresh: true)
    let fixtures = hlsrBackendFixtures()
    await backend.paper.prewarm(MarketSnapshot(instrumentID: "ALT-USDT-SWAP", interval: .fourHours, candles: fixtures.fourHour))
    for candle in fixtures.lower {
        _ = await backend.ingestRealtimeCandle(candle, instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    }
    let places = await runner.commandsContainingPlace()
    #expect(places.count == 1)
    let place = try #require(places.first)
    let index = try #require(place.firstIndex(of: "--clOrdId"))
    return (backend, config, place[index + 1])
}

@Test("A strategy entry with an unknown outcome keeps its pool slot and ownership until OKX confirms it")
func strategyEntryWithUnknownOutcomeKeepsPoolSlotUntilAccepted() async throws {
    let runner = HLSRBackendRunner()
    let directory = backendDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let (backend, config, clientOrderID) = try await submitHLSREntryWithUnknownOutcome(runner: runner, directory: directory)

    let entries = (await backend.paper.allOrders()).filter { $0.strategyID == config.id }
    #expect(entries.count == 1)
    #expect(entries.first?.remoteOrderID == nil)
    #expect(entries.first?.status == "submitted")
    let pool = await backend.riskEngine.strategyCapital(config.id)
    #expect(pool.openPositions == 1)
    #expect(pool.reservedCapital > 0)

    await runner.setClientOrderLookupFails(false)
    await runner.setClientOrder(clientOrderID, order: OrderSnapshot(id: "late-entry", instrumentID: "ALT-USDT-SWAP", side: "sell", status: "filled", quantity: 1))
    _ = try await backend.account()
    #expect((await backend.paper.allOrders()).first(where: { $0.strategyID == config.id })?.remoteOrderID == "late-entry")
    let confirmed = await backend.riskEngine.strategyCapital(config.id)
    #expect(confirmed.openPositions == 1)
    #expect(confirmed.reservedCapital == pool.reservedCapital)
    let reservations = try JSONSerialization.jsonObject(with: Data(contentsOf: directory.appendingPathComponent("remote-reservations.json"))) as? [String: Any]
    #expect(reservations.map { Set($0.keys) } == ["late-entry"])
}

@Test("A strategy entry that OKX never received releases its pool slot after the lookup answers")
func strategyEntryWithUnknownOutcomeReleasesPoolWhenAbsent() async throws {
    let runner = HLSRBackendRunner()
    let directory = backendDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let (backend, config, _) = try await submitHLSREntryWithUnknownOutcome(runner: runner, directory: directory)
    #expect((await backend.riskEngine.strategyCapital(config.id)).openPositions == 1)

    // Deleting the strategy must wait for the outcome.
    _ = try await backend.pauseStrategy(config.id)
    await #expect(throws: ATKError.unavailable("策略仍有结果未知的下单，请等待后台按 clOrdId 核对完成后再删除")) {
        _ = try await backend.deleteStrategy(config.id)
    }

    await runner.setClientOrderLookupFails(false) // lookup now returns 51603
    _ = try await backend.account()
    let pool = await backend.riskEngine.strategyCapital(config.id)
    #expect(pool.openPositions == 0)
    #expect(pool.reservedCapital == 0)
    #expect(pool.openRisk == 0)
    #expect((await backend.paper.allOrders()).first(where: { $0.strategyID == config.id })?.status == "rejected")
}

private func hlsrBackendFixtures() -> (lower: [Candle], fourHour: [Candle]) {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    var lower: [Candle] = []
    for index in 0..<103 {
        let timestamp = start.addingTimeInterval(Double(index) * 15 * 60)
        let value = 65.0 + Double(index) * 0.37
        switch index {
        case 100:
            lower.append(Candle(timestamp: timestamp, open: 100, high: 108, low: 98, close: 99, volume: 1, quoteVolume: 500_000))
        case 101:
            lower.append(Candle(timestamp: timestamp, open: 100, high: 102, low: 96, close: 98, volume: 1, quoteVolume: 400_000))
        case 102:
            lower.append(Candle(timestamp: timestamp, open: 97, high: 98, low: 95, close: 97, volume: 1, quoteVolume: 100_000, confirmed: false))
        default:
            lower.append(Candle(timestamp: timestamp, open: Decimal(value - 0.1), high: Decimal(value + 0.4), low: Decimal(value - 0.4), close: Decimal(value), volume: 1, quoteVolume: 400_000))
        }
    }
    var fourHour: [Candle] = []
    for index in 0..<62 {
        let timestamp = start.addingTimeInterval(Double(index - 55) * 4 * 3600)
        let close = 200.0 - Double(index) * 1.8
        fourHour.append(Candle(timestamp: timestamp, open: Decimal(close + 1), high: Decimal(close + 3), low: Decimal(close - 3), close: Decimal(close), volume: 1, quoteVolume: 1_000_000))
    }
    return (lower, fourHour)
}
