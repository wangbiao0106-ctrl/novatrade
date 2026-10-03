import Foundation
import Testing
import ATKGateway
import TradingDomain
@testable import TradingService

@Test
func strategyScopesResolveSingleMultipleAndDynamicTargets() throws {
    let contracts = [
        ContractMarket(id: "BTC-USDT-SWAP", name: "BTC", baseCurrency: "BTC", quoteCurrency: "USDT", last: 100, changePercent: 2, volume24h: 1_000_000_000),
        ContractMarket(id: "ALT-USDT-SWAP", name: "ALT", baseCurrency: "ALT", quoteCurrency: "USDT", last: 1, changePercent: 60, volume24h: 50_000_000),
        ContractMarket(id: "MOON-USDT-SWAP", name: "MOON", baseCurrency: "MOON", quoteCurrency: "USDT", last: 1, changePercent: 101, volume24h: 40_000_000)
    ]
    #expect(StrategyScope.single("BTC-USDT-SWAP").resolvedInstrumentIDs(from: contracts) == ["BTC-USDT-SWAP"])
    #expect(StrategyScope.multiple(["BTC-USDT-SWAP", "ALT-USDT-SWAP"]).resolvedInstrumentIDs(from: contracts).count == 2)
    #expect(StrategyScope.dynamic(.hotAltcoins).resolvedInstrumentIDs(from: contracts) == ["ALT-USDT-SWAP", "MOON-USDT-SWAP"])
}

@Test
func strategySpecificScopesDoNotReuseTheHot20Pool() {
    let contracts = [
        ContractMarket(id: "SWEEP-USDT-SWAP", name: "SWEEP", baseCurrency: "SWEEP", quoteCurrency: "USDT", last: 1, changePercent: 5, volume24h: 80_000_000),
        ContractMarket(id: "DME-USDT-SWAP", name: "DME", baseCurrency: "DME", quoteCurrency: "USDT", last: 1, changePercent: 101, volume24h: 10_000_000),
        ContractMarket(id: "DME-LOW-VOLUME-USDT-SWAP", name: "DME low volume", baseCurrency: "DME-LOW-VOLUME", quoteCurrency: "USDT", last: 1, changePercent: 120, volume24h: 9_000_000),
    ]

    #expect(StrategyType.sweepReversalShort.defaultUniverseCategory == .sweepCandidates)
    #expect(StrategyType.doublePumpExhaustionShort.defaultUniverseCategory == .doublePumpCandidates)
    #expect(StrategyScope.dynamic(.doublePumpCandidates).resolvedInstrumentIDs(from: contracts) == ["DME-USDT-SWAP"])
}

@Test
func paperStoreReportsAndRefreshesTheConcreteScanPool() async throws {
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("novatrade-universe-cache-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }

    let store = PaperTradingStore(directory: directory)
    let config = StrategyConfig(name: "DME", scope: .dynamic(.doublePumpCandidates),
                                interval: .fifteenMinutes, type: .doublePumpExhaustionShort)
    _ = try await store.create(config)

    let eligible = ContractMarket(id: "TARGET-USDT-SWAP", name: "TARGET", baseCurrency: "TARGET",
                                  quoteCurrency: "USDT", last: 1, changePercent: 101, rollingChangePercent: 101, volume24h: 10_000_000)
    let excluded = ContractMarket(id: "EXCLUDED-USDT-SWAP", name: "EXCLUDED", baseCurrency: "EXCLUDED",
                                  quoteCurrency: "USDT", last: 1, changePercent: 101, rollingChangePercent: 101, volume24h: 9_000_000)
    await store.refreshStrategyUniverse([eligible, excluded])
    let first = await store.strategyUniverseTargets()
    #expect(first.count == 1)
    #expect(first[0].instrumentIDs == [eligible.id])
    #expect(first[0].targetCount == 1)

    let replacement = ContractMarket(id: "REPLACEMENT-USDT-SWAP", name: "REPLACEMENT", baseCurrency: "REPLACEMENT",
                                     quoteCurrency: "USDT", last: 1, changePercent: 101, rollingChangePercent: 101, volume24h: 10_000_000)
    await store.refreshStrategyUniverse([replacement])
    let second = await store.strategyUniverseTargets()
    #expect(second[0].instrumentIDs == [replacement.id])
    #expect(second[0].instrumentIDs.contains(eligible.id) == false)
}

@Test
func unknownStrategyTypeDoesNotDiscardLedgerOrKnownStrategies() async throws {
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("novatrade-unknown-strategy-state-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }

    let knownID = UUID()
    let orphanID = UUID()
    let orderID = UUID()
    let known = StrategyConfig(id: knownID, name: "Sweep", scope: .dynamic(.hotAltcoins),
                               interval: .oneHour, type: .sweepReversalShort)
    let orphan = StrategyConfig(id: orphanID, name: "实验室策略", scope: .dynamic(.hotAltcoins),
                                interval: .oneHour, type: .external("lab-new-v9"), enabled: true)
    let order = PaperOrder(id: orderID, strategyID: orphanID, instrumentID: "ALT-USDT-SWAP",
                           side: "short", quantity: 2, status: "filled")
    let fill = PaperFill(orderID: orderID, price: 12, quantity: 2, fee: 0.02)
    let risk = RiskSnapshot(equity: 10_000, equityPeak: 10_500, dayStartEquity: 9_900,
                            dailyPnLPercent: 1, drawdownPercent: 2)

    struct State: Codable {
        let schemaVersion: Int
        let strategies: [StrategyConfig]
        let statuses: [String: StrategyStatus]
        let orders: [PaperOrder]
        let fills: [PaperFill]
        let risk: RiskSnapshot
    }
    let state = State(schemaVersion: 1,
                      strategies: [known, orphan],
                      statuses: [knownID.uuidString: StrategyStatus(id: knownID, state: .running),
                                 orphanID.uuidString: StrategyStatus(id: orphanID, state: .running)],
                      orders: [order], fills: [fill], risk: risk)
    let encoder = JSONEncoder()
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    try encoder.encode(state).write(to: directory.appendingPathComponent("paper-state.json"))

    let store = PaperTradingStore(directory: directory)
    let strategies = await store.allStrategies()
    #expect(strategies.contains(where: { $0.id == knownID && $0.type == .sweepReversalShort }))
    #expect(strategies.contains(where: { $0.id == orphanID && $0.type == .external("lab-new-v9") && !$0.enabled }))
    #expect((await store.allOrders()).contains(where: { $0.id == orderID }))
    #expect((await store.allFills()).contains(where: { $0.orderID == orderID }))
    #expect((await store.riskSnapshot()).equity == 10_000)
    #expect((await store.status(for: orphanID))?.state == .paused)
}

@Test
func storeCanonicalizesEveryRuntimeStrategyToItsOwnUniverse() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-strategy-universe-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = PaperTradingStore(directory: directory)

    let dme = try await store.create(StrategyConfig(name: "DME", scope: .dynamic(.hotAltcoins), interval: .fifteenMinutes, type: .doublePumpExhaustionShort))
    #expect(dme.scope == .dynamic(.doublePumpCandidates))
}

@Test("Strategy store keeps a user-selected leverage override")
func strategyStorePreservesCustomLeverage() async throws {
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("novatrade-strategy-leverage-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = PaperTradingStore(directory: directory)
    var parameters = StrategyType.sweepReversalShort.defaultParameters
    parameters["leverage"] = 3
    let input = StrategyConfig(name: StrategyType.sweepReversalShort.displayName,
                               scope: .dynamic(.hotAltcoins), interval: .oneHour,
                               type: .sweepReversalShort, parameters: parameters)

    let created = try await store.create(input)
    #expect(created.parameters["leverage"] == 3)

    var updated = created
    updated.parameters["leverage"] = 1
    let saved = try await store.update(updated)
    #expect(saved.parameters["leverage"] == 1)
}

@Test("Strategy store rejects leverage outside the supported range")
func strategyStoreRejectsInvalidLeverage() async throws {
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("novatrade-strategy-invalid-leverage-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = PaperTradingStore(directory: directory)
    var parameters = StrategyType.sweepReversalShort.defaultParameters
    parameters["leverage"] = 100.5
    do {
        _ = try await store.create(StrategyConfig(name: StrategyType.sweepReversalShort.displayName,
                                                   scope: .dynamic(.hotAltcoins), interval: .oneHour,
                                                   type: .sweepReversalShort, parameters: parameters))
        Issue.record("expected leverage above 100x to be rejected")
    } catch PaperTradingStore.StoreError.unsupported {
        // Expected.
    }
}

@Test
func strategyStoreRejectsFixedInstrumentScopes() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-single-scope-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = PaperTradingStore(directory: directory)

    let invalidScopes = [
        StrategyScope.single(""),
        StrategyScope.single("ALT-USDT-SWAP"),
        StrategyScope.multiple(["ALT-USDT-SWAP", "ALT2-USDT-SWAP"]),
    ]
    for scope in invalidScopes {
        do {
            _ = try await store.create(StrategyConfig(name: "无效范围", scope: scope, interval: .oneHour, type: .sweepReversalShort, riskPercent: 0.5))
            Issue.record("expected a strategy scope with anything other than one instrument to be rejected")
        } catch PaperTradingStore.StoreError.unsupported {
            // Expected.
        }
    }
}

@Test
func hotAltcoinScopeExcludesNonTargetsAndCapsAtTwenty() {
    let ranked = (0..<21).map { index in
        ContractMarket(id: "ALT\(index)-USDT-SWAP", name: "ALT\(index)", baseCurrency: "ALT\(index)", quoteCurrency: "USDT", last: 1, volume24h: Decimal(21_000 - index))
    }
    let excluded = [
        ContractMarket(id: "BTC-USDT-SWAP", name: "BTC", baseCurrency: "BTC", quoteCurrency: "USDT", last: 1, volume24h: 99_999),
        ContractMarket(id: "AAPL-USDT-SWAP", name: "AAPL", baseCurrency: "AAPL", quoteCurrency: "USDT", last: 1, volume24h: 99_998),
        ContractMarket(id: "USDC-USDT-SWAP", name: "USDC", baseCurrency: "USDC", quoteCurrency: "USDT", last: 1, volume24h: 99_997),
        ContractMarket(id: "ALT-BTC-SWAP", name: "ALT/BTC", baseCurrency: "ALT", quoteCurrency: "BTC", last: 1, volume24h: 99_996)
    ]
    let ids = StrategyScope.dynamic(.hotAltcoins).resolvedInstrumentIDs(from: ranked + excluded)
    #expect(ids.count == 20)
    #expect(ids.first == "ALT0-USDT-SWAP")
    #expect(!ids.contains("BTC-USDT-SWAP"))
    #expect(!ids.contains("AAPL-USDT-SWAP"))
    #expect(!ids.contains("USDC-USDT-SWAP"))
    #expect(!ids.contains("ALT-BTC-SWAP"))
}

@Test
func sweepCandidateScopeAppliesTurnoverFloorAndCapsAtOneHundred() {
    // 110 eligible altcoins at or above the 3M USDT floor, ranked by turnover.
    let ranked = (0..<110).map { index in
        ContractMarket(id: "ALT\(index)-USDT-SWAP", name: "ALT\(index)", baseCurrency: "ALT\(index)", quoteCurrency: "USDT", last: 1, volume24h: Decimal(3_000_000 + (110 - index) * 10_000))
    }
    let atFloor = ContractMarket(id: "FLOOR-USDT-SWAP", name: "FLOOR", baseCurrency: "FLOOR", quoteCurrency: "USDT", last: 1, volume24h: 3_000_000)
    let belowFloor = ContractMarket(id: "THIN-USDT-SWAP", name: "THIN", baseCurrency: "THIN", quoteCurrency: "USDT", last: 1, volume24h: 2_999_999)
    let excluded = [
        ContractMarket(id: "BTC-USDT-SWAP", name: "BTC", baseCurrency: "BTC", quoteCurrency: "USDT", last: 1, volume24h: 9_000_000_000),
        ContractMarket(id: "AAPL-USDT-SWAP", name: "AAPL", baseCurrency: "AAPL", quoteCurrency: "USDT", last: 1, volume24h: 900_000_000),
        ContractMarket(id: "ALT-BTC-SWAP", name: "ALT/BTC", baseCurrency: "ALT", quoteCurrency: "BTC", last: 1, volume24h: 800_000_000)
    ]
    let full = StrategyScope.dynamic(.sweepCandidates).resolvedInstrumentIDs(from: excluded + [belowFloor] + ranked.reversed())
    #expect(full.count == StrategyUniverseRules.sweepCandidateLimit)
    #expect(full == ranked.prefix(100).map(\.id))
    #expect(!full.contains("BTC-USDT-SWAP"))
    #expect(!full.contains("AAPL-USDT-SWAP"))
    #expect(!full.contains("ALT-BTC-SWAP"))

    // In a thin market the floor, not the cap, decides the pool size.
    let thin = StrategyScope.dynamic(.sweepCandidates).resolvedInstrumentIDs(from: Array(ranked.prefix(5)) + [atFloor, belowFloor])
    #expect(thin == ranked.prefix(5).map(\.id) + ["FLOOR-USDT-SWAP"])
}

@Test
func strategyStoreAllowsOneInstancePerRuleAndDeletesIt() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-strategy-store-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = PaperTradingStore(directory: directory)
    #expect((await store.allStrategies()).isEmpty)

    let config = StrategyConfig(name: "山寨币二次扫顶做空", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort)
    _ = try await store.create(config)
    do {
        _ = try await store.create(StrategyConfig(name: "重复扫顶", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort))
        Issue.record("expected duplicate strategy rule to be rejected")
    } catch PaperTradingStore.StoreError.conflict {
        // Expected.
    }
    _ = try await store.setState(config.id, running: true)
    do {
        _ = try await store.delete(config.id)
        Issue.record("expected running strategy deletion to be rejected")
    } catch PaperTradingStore.StoreError.running {
        // Expected.
    }
    _ = try await store.setState(config.id, running: false)
    _ = try await store.delete(config.id)
    #expect((await store.allStrategies()).isEmpty)
}

@Test
func tradingBackendCannotRestartStrategyWhileAccountKillSwitchIsLatched() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-kill-switch-start-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let risk = RiskEngine(initialEquity: 1_000)
    await risk.synchronizeStrategyCapital(1_000)
    let backend = TradingBackend(paper: PaperTradingStore(directory: directory), riskEngine: risk)
    let config = try await backend.createStrategy(StrategyConfig(name: "山寨币二次扫顶做空", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort))
    let lossTime = Date(timeIntervalSince1970: 1_700_000_000)
    await risk.record(realizedPnL: -60, now: lossTime)

    do {
        _ = try await backend.startStrategy(config.id)
        Issue.record("expected strategy start to be blocked by the account kill switch")
    } catch {
        #expect(error.localizedDescription.contains("熔断"))
    }
}

@Test
func tradingBackendAllowsSavingPausedStrategyWhileAccountKillSwitchIsLatched() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-kill-switch-save-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let risk = RiskEngine(initialEquity: 1_000)
    await risk.synchronizeStrategyCapital(1_000)
    let backend = TradingBackend(paper: PaperTradingStore(directory: directory), riskEngine: risk)
    let lossTime = Date(timeIntervalSince1970: 1_700_000_000)
    await risk.record(realizedPnL: -60, now: lossTime)
    #expect((await risk.snapshot()).killSwitch)

    let paused = try await backend.createStrategy(StrategyConfig(
        name: "熔断期间保存",
        scope: .dynamic(.hotAltcoins),
        interval: .oneHour,
        type: .sweepReversalShort,
        enabled: false
    ))
    #expect(!paused.enabled)
    #expect((await backend.strategies()).contains(where: { $0.id == paused.id }))

    do {
        _ = try await backend.createStrategy(StrategyConfig(
            name: "熔断期间启动",
            scope: .dynamic(.hotAltcoins),
            interval: .fifteenMinutes,
            type: .doublePumpExhaustionShort,
            enabled: true,
            riskPercent: 0.5
        ))
        Issue.record("expected enabled strategy creation to be blocked by the account kill switch")
    } catch {
        #expect(error.localizedDescription.contains("熔断"))
    }
}

@Test
func deletingStrategyPersistsRemovalOfItsCapitalPool() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-strategy-pool-delete-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }

    let firstRisk = RiskEngine(initialEquity: 10_000)
    await firstRisk.synchronizeStrategyCapital(10_000)
    let first = TradingBackend(paper: PaperTradingStore(directory: directory), riskEngine: firstRisk)
    let config = StrategyConfig(name: "山寨币二次扫顶做空", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort)
    let created = try await first.createStrategy(config)
    #expect((await first.strategyCapital()).contains { $0.strategyID == created.id })
    _ = try await first.deleteStrategy(created.id)

    let secondRisk = RiskEngine(initialEquity: 10_000)
    await secondRisk.synchronizeStrategyCapital(10_000)
    let second = TradingBackend(paper: PaperTradingStore(directory: directory), riskEngine: secondRisk)
    #expect((await second.strategyCapital()).isEmpty)
}

@Test
func strategyStoreAcceptsTenPercentRiskAndRejectsAboveIt() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-strategy-risk-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = PaperTradingStore(directory: directory)
    let accepted = StrategyConfig(name: "上限内", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort, riskPercent: 10.0)
    let created = try await store.create(accepted)
    #expect(created.riskPercent == 10.0)
    #expect(created.parameters["maxOpenRiskPercent"] == 10.0)
    let config = StrategyConfig(name: "超限", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort, riskPercent: 10.1)
    do {
        _ = try await store.create(config)
        Issue.record("expected risk above ten percent of the pool to be rejected")
    } catch PaperTradingStore.StoreError.unsupported {
        // Expected.
    }
}

@Test
func candleStoreReplacesUnconfirmedCandleAndKeepsOrder() async {
    let store = CandleStore()
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    let first = Candle(timestamp: start, open: 10, high: 12, low: 9, close: 11, confirmed: false)
    let correction = Candle(timestamp: start, open: 10, high: 13, low: 9, close: 12, confirmed: true)
    let earlier = Candle(timestamp: start.addingTimeInterval(-60), open: 8, high: 10, low: 7, close: 9)
    await store.ingest([first, earlier], instrumentID: "BTC-USDT-SWAP", interval: .oneMinute)
    await store.ingest(correction, instrumentID: "BTC-USDT-SWAP", interval: .oneMinute)
    let values = await store.values(instrumentID: "BTC-USDT-SWAP", interval: .oneMinute)
    #expect(values.count == 2)
    #expect(values[0].timestamp < values[1].timestamp)
    #expect(values[1] == correction)
}

@Test
func candleStoreNeverDowngradesConfirmedCandle() async {
    let store = CandleStore()
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    let confirmed = Candle(timestamp: start, open: 10, high: 13, low: 9, close: 12, confirmed: true)
    // A REST snapshot fetched before the bar closed arrives late.
    let stale = Candle(timestamp: start, open: 10, high: 12, low: 9, close: 11, confirmed: false)
    await store.ingest(confirmed, instrumentID: "BTC-USDT-SWAP", interval: .oneMinute)
    await store.ingest([stale], instrumentID: "BTC-USDT-SWAP", interval: .oneMinute)
    let values = await store.values(instrumentID: "BTC-USDT-SWAP", interval: .oneMinute)
    #expect(values == [confirmed])
}

@Test
func paperBrokerFillsOnlyOnFollowingCandleAndAppliesFeeAndSlippage() async throws {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0.001, slippageBps: 10)
    let strategyID = UUID()
    let signalTime = Date(timeIntervalSince1970: 1_700_000_000)
    let submitted = await broker.submit(strategyID: strategyID, instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 1, referencePrice: 100, requestedAt: signalTime)
    guard case .success = submitted else { Issue.record("expected paper order to pass risk checks"); return }

    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: signalTime, open: 101, high: 102, low: 100, close: 101))
    #expect(await broker.allFills().isEmpty)
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: signalTime.addingTimeInterval(60), open: 101, high: 102, low: 100, close: 101))
    let fills = await broker.allFills()
    #expect(fills.count == 1)
    #expect(fills[0].price == Decimal(string: "101.101"))
    #expect(fills[0].fee == Decimal(string: "0.101101"))
}

@Test
func paperBrokerKeepsRiskPositionCountConsistentAcrossScaleAndReversal() async throws {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let strategyID = UUID()
    _ = await risk.registerStrategy(strategyID, allocationPercent: 100)
    let t0 = Date(timeIntervalSince1970: 1_700_000_000)

    guard case .success = await broker.submit(strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "long", quantity: 10, referencePrice: 100, requestedAt: t0) else {
        Issue.record("initial entry should be accepted")
        return
    }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: t0.addingTimeInterval(60), open: 100, high: 100, low: 100, close: 100))

    // A normal opposite-side order partially closes the position. It must not
    // consume a second concurrent-position slot.
    guard case .success = await broker.submit(strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 4, referencePrice: 100, requestedAt: t0.addingTimeInterval(120)) else {
        Issue.record("partial close should be accepted")
        return
    }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: t0.addingTimeInterval(180), open: 100, high: 100, low: 100, close: 100))
    var capital = await risk.strategyCapital(strategyID)
    #expect(capital.openPositions == 1)
    #expect(capital.reservedCapital == 600)

    // Reversing by more than the residual closes the old slot and leaves one
    // slot for the residual short position.
    guard case .success = await broker.submit(strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 8, referencePrice: 100, requestedAt: t0.addingTimeInterval(240)) else {
        Issue.record("reversal should be accepted")
        return
    }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: t0.addingTimeInterval(300), open: 100, high: 100, low: 100, close: 100))
    capital = await risk.strategyCapital(strategyID)
    #expect(capital.openPositions == 1)
    #expect(capital.reservedCapital == 200)
    #expect((await broker.allPositions()).first?.side == "short")
    #expect((await broker.allPositions()).first?.quantity == 2)
}

@Test
func paperBrokerReduceOnlyFullCloseReleasesPositionSlot() async throws {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let strategyID = UUID()
    _ = await risk.registerStrategy(strategyID, allocationPercent: 100)
    let t0 = Date(timeIntervalSince1970: 1_700_000_100)

    guard case .success = await broker.submit(strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "long", quantity: 2, referencePrice: 100, requestedAt: t0) else {
        Issue.record("entry should be accepted")
        return
    }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: t0.addingTimeInterval(60), open: 100, high: 100, low: 100, close: 100))
    guard case .success = await broker.submit(strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "short", quantity: 2, referencePrice: 100, requestedAt: t0.addingTimeInterval(120), reduceOnly: true) else {
        Issue.record("reduce-only close should be accepted")
        return
    }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: t0.addingTimeInterval(180), open: 100, high: 100, low: 100, close: 100))
    let capital = await risk.strategyCapital(strategyID)
    #expect(capital.openPositions == 0)
    #expect(capital.reservedCapital == 0)
    #expect((await broker.allPositions()).isEmpty)
}

@Test
func paperBrokerTracksAndReleasesStopRiskReservations() async throws {
    let risk = RiskEngine(limits: RiskLimits(maxMarginPercent: 100, minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let strategyID = UUID()
    _ = await risk.registerStrategy(strategyID, allocationPercent: 100)
    let base = Date(timeIntervalSince1970: 1_700_000_200)

    guard case .success = await broker.submit(
        strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "long",
        quantity: 1, referencePrice: 100, requestedAt: base,
        riskAmount: 400, maxOpenRiskPercent: 5, maxConcurrentPositions: 2
    ) else {
        Issue.record("entry with a valid stop-risk budget should be accepted")
        return
    }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(60), open: 100, high: 100, low: 100, close: 100))
    #expect((await risk.strategyCapital(strategyID)).openRisk == Decimal(400))

    let overRisk = await broker.submit(
        strategyID: strategyID, instrumentID: "OTHER-USDT-SWAP", side: "long",
        quantity: 1, referencePrice: 100, requestedAt: base.addingTimeInterval(120),
        riskAmount: 101, maxOpenRiskPercent: 5, maxConcurrentPositions: 2
    )
    guard case .failure(let error) = overRisk else {
        Issue.record("the aggregate stop-risk cap should reject the second entry")
        return
    }
    #expect(error.localizedDescription == "策略开放止损风险超过上限")

    guard case .success = await broker.submit(
        strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "short",
        quantity: 1, referencePrice: 100, requestedAt: base.addingTimeInterval(180),
        reduceOnly: true
    ) else {
        Issue.record("reduce-only close should remain available")
        return
    }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(240), open: 100, high: 100, low: 100, close: 100))
    let pool = await risk.strategyCapital(strategyID)
    #expect(pool.openRisk == 0)
    #expect(pool.openPositions == 0)
}

@Test
func paperBrokerReversalRetainsOnlyResidualStopRisk() async throws {
    let risk = RiskEngine(limits: RiskLimits(maxMarginPercent: 100, minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let strategyID = UUID()
    let base = Date(timeIntervalSince1970: 1_700_000_300)

    guard case .success = await broker.submit(
        strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "long",
        quantity: 10, referencePrice: 100, requestedAt: base,
        riskAmount: 100, maxOpenRiskPercent: 5, maxConcurrentPositions: 2
    ) else {
        Issue.record("first controlled order should register its strategy pool")
        return
    }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(60), open: 100, high: 100, low: 100, close: 100))

    guard case .success = await broker.submit(
        strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "short",
        quantity: 4, referencePrice: 100, requestedAt: base.addingTimeInterval(120),
        reduceOnly: true
    ) else { Issue.record("partial close should be accepted"); return }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(180), open: 100, high: 100, low: 100, close: 100))
    #expect((await risk.strategyCapital(strategyID)).openRisk == Decimal(60))

    guard case .success = await broker.submit(
        strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "short",
        quantity: 8, referencePrice: 100, requestedAt: base.addingTimeInterval(240),
        riskAmount: 80, maxOpenRiskPercent: 5, maxConcurrentPositions: 2
    ) else { Issue.record("reversal should be accepted"); return }
    await broker.processNextOpen(instrumentID: "ALT-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(300), open: 100, high: 100, low: 100, close: 100))
    let reversed = await risk.strategyCapital(strategyID)
    #expect(reversed.openRisk == Decimal(20))
    #expect(reversed.openPositions == 1)

    _ = await broker.flattenAll(at: base.addingTimeInterval(360))
    let flat = await risk.strategyCapital(strategyID)
    #expect(flat.openRisk == 0)
    #expect(flat.openPositions == 0)
}

@Test
func paperBrokerCancellationReleasesStopRiskAndPositionSlot() async throws {
    let risk = RiskEngine(limits: RiskLimits(maxMarginPercent: 100, minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let strategyID = UUID()
    guard case .success = await broker.submit(
        strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", side: "long",
        quantity: 1, referencePrice: 100, riskAmount: 400,
        maxOpenRiskPercent: 5, maxConcurrentPositions: 1
    ) else { Issue.record("controlled paper entry should be accepted"); return }

    await broker.cancelPendingOrders(strategyID: strategyID)
    let pool = await risk.strategyCapital(strategyID)
    #expect(pool.openRisk == 0)
    #expect(pool.openPositions == 0)
    #expect(pool.reservedCapital == 0)
    #expect((await broker.allOrders()).first?.status == "cancelled")
}

@Test
func riskEngineEnforcesNotionalAndThrottleLimits() async {
    let risk = RiskEngine(limits: RiskLimits(maxInstrumentNotional: 100, minOrderIntervalSeconds: 10), initialEquity: 10_000)
    let now = Date(timeIntervalSince1970: 1_700_000_000)
    let first = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 50, margin: 50, now: now)
    let throttled = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 10, margin: 10, now: now.addingTimeInterval(1))
    let capped = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 60, margin: 60, now: now.addingTimeInterval(11))
    #expect(first.allowed)
    #expect(!throttled.allowed)
    #expect(capped.reason == "单标的名义价值超过上限")
}

@Test
func riskLimitsNormalizeInvalidCeilingsToFailClosedValues() {
    let limits = RiskLimits(maxInstrumentNotional: -1, maxTotalNotional: -2, maxMarginPercent: -3,
                            minOrderIntervalSeconds: -4, maxOrdersPerHour: -5,
                            maxDailyLossPercent: -6, maxDrawdownPercent: -7)
    #expect(limits.maxInstrumentNotional == 0)
    #expect(limits.maxTotalNotional == 0)
    #expect(limits.maxMarginPercent == 0)
    #expect(limits.minOrderIntervalSeconds == 0)
    #expect(limits.maxOrdersPerHour == 0)
    #expect(limits.maxDailyLossPercent == 0)
    #expect(limits.maxDrawdownPercent == 0)
}

@Test
func riskEnginePersistsGlobalNotionalAcrossRestart() async {
    let limits = RiskLimits(maxInstrumentNotional: 100, minOrderIntervalSeconds: 0)
    let first = RiskEngine(limits: limits, initialEquity: 10_000)
    #expect((await first.authorize(instrumentID: "BTC-USDT-SWAP", notional: 80, margin: 80)).allowed)
    let saved = await first.snapshot()
    let restored = RiskEngine(limits: limits, initialEquity: 10_000)
    await restored.restore(saved)
    let rejected = await restored.authorize(instrumentID: "BTC-USDT-SWAP", notional: 30, margin: 30)
    #expect(rejected.reason == "单标的名义价值超过上限")
}

@Test
func strategyRiskEngineAllowsOnlyOneActiveSymbol() async {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let strategyID = UUID()
    let first = await risk.authorize(
        instrumentID: "ALT-USDT-SWAP", notional: 100, margin: 100,
        strategyID: strategyID, poolAllocationPercent: 100, riskAmount: 5,
        maxOpenRiskPercent: 5, maxConcurrentPositions: 1
    )
    let second = await risk.authorize(
        instrumentID: "ALT2-USDT-SWAP", notional: 100, margin: 100,
        strategyID: strategyID, poolAllocationPercent: 100, riskAmount: 5,
        maxOpenRiskPercent: 5, maxConcurrentPositions: 1
    )
    #expect(first.allowed)
    #expect(!second.allowed)
    #expect(second.reason == "策略并发持仓超过上限")
}

@Test
func riskEngineRejectsNegativeRiskInputsAndDoesNotIncreaseReservationsOnRelease() async {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let strategyID = UUID()
    _ = await risk.registerStrategy(strategyID, allocationPercent: 100)
    let negativeRisk = await risk.authorize(
        instrumentID: "ALT-USDT-SWAP", notional: 100, margin: 100,
        strategyID: strategyID, riskAmount: -1, maxOpenRiskPercent: 5
    )
    #expect(!negativeRisk.allowed)
    #expect(negativeRisk.reason == "止损风险必须是有限的非负数")
    let baseline = await risk.strategyCapital(strategyID)
    await risk.release(instrumentID: "ALT-USDT-SWAP", notional: -100, strategyID: strategyID, margin: -100, riskAmount: -1)
    let after = await risk.strategyCapital(strategyID)
    #expect(after.reservedCapital == baseline.reservedCapital)
    #expect(after.openRisk == baseline.openRisk)
    #expect(after.openPositions == baseline.openPositions)

    let zeroCaps = await risk.authorize(
        instrumentID: "ALT-USDT-SWAP", notional: 100, margin: 100,
        strategyID: strategyID, riskAmount: 1, maxOpenRiskPercent: 0, maxConcurrentPositions: 0
    )
    #expect(!zeroCaps.allowed)
}

@Test
func riskEngineRollsDailyBaselineWhenUTCDayChanges() async {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(secondsFromGMT: 0)!
    let dayOne = calendar.date(from: DateComponents(year: 2026, month: 9, day: 1, hour: 23, minute: 0))!
    let dayTwo = calendar.date(from: DateComponents(year: 2026, month: 9, day: 2, hour: 0, minute: 1))!

    let risk = RiskEngine(initialEquity: 1000)
    await risk.record(realizedPnL: -60, now: dayOne)
    let beforeRollover = await risk.snapshot(now: dayOne)
    #expect(beforeRollover.dailyPnLPercent == Decimal(-6))
    let afterRollover = await risk.snapshot(now: dayTwo)
    // The daily baseline resets at midnight so yesterday's loss does not
    // count against today's limit.
    #expect(afterRollover.dayStartEquity == Decimal(940))
    #expect(afterRollover.dailyPnLPercent == 0)
}

@Test
func riskEngineUsesUTCMidnightInsteadOfLocalMidnight() async {
    var shanghai = Calendar(identifier: .gregorian)
    shanghai.timeZone = TimeZone(identifier: "Asia/Shanghai")!
    // These instants are on the same Shanghai calendar day but straddle
    // midnight UTC (23:59Z -> 00:01Z).
    let beforeUTCMidnight = shanghai.date(from: DateComponents(year: 2026, month: 9, day: 2, hour: 7, minute: 59))!
    let afterUTCMidnight = shanghai.date(from: DateComponents(year: 2026, month: 9, day: 2, hour: 8, minute: 1))!

    let risk = RiskEngine(initialEquity: 1000)
    await risk.record(realizedPnL: -60, now: beforeUTCMidnight)
    let afterRollover = await risk.snapshot(now: afterUTCMidnight)
    #expect(afterRollover.dayStartEquity == Decimal(940))
    #expect(afterRollover.dailyPnLPercent == 0)
}

@Test
func riskEngineRestoresPriorDayKillAndAllowsResetOnTheNewDay() async {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(secondsFromGMT: 0)!
    let dayOne = calendar.date(from: DateComponents(year: 2026, month: 9, day: 1, hour: 23))!
    let dayTwo = calendar.date(from: DateComponents(year: 2026, month: 9, day: 2, hour: 0, minute: 1))!

    let source = RiskEngine(initialEquity: 1_000)
    await source.record(realizedPnL: -60, now: dayOne)
    let persisted = await source.snapshot(now: dayOne)
    #expect(persisted.killSwitch)
    #expect(persisted.dayStartAt != nil)

    let restored = RiskEngine(initialEquity: 100_000)
    await restored.restore(persisted, now: dayTwo)
    let afterRestart = await restored.snapshot(now: dayTwo)
    #expect(afterRestart.dayStartEquity == Decimal(940))
    #expect(afterRestart.dailyPnLPercent == 0)
    #expect(afterRestart.killSwitch)

    let reset = await restored.resetKillSwitch(now: dayTwo)
    #expect(!reset.killSwitch)
    #expect(reset.dayStartEquity == Decimal(940))
}

@Test
func riskEngineTripwiresDailyLossLimit() async {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(secondsFromGMT: 0)!
    let dayOne = calendar.date(from: DateComponents(year: 2026, month: 9, day: 1, hour: 23, minute: 0))!
    let risk = RiskEngine(initialEquity: 1000)
    await risk.record(realizedPnL: -20, now: dayOne)
    #expect(!(await risk.snapshot(now: dayOne)).killSwitch)
    await risk.record(realizedPnL: -40, now: dayOne.addingTimeInterval(60))
    let snapshot = await risk.snapshot(now: dayOne.addingTimeInterval(120))
    #expect(snapshot.killSwitch)
    #expect(snapshot.reason == "单日亏损熔断")
}

@Test
func riskEngineTripwiresDailyLossOnMarkToMarketEquity() async {
    let risk = RiskEngine(initialEquity: 10_000)
    let now = Date(timeIntervalSince1970: 1_700_000_000)
    await risk.markToMarket(unrealizedPnL: -500, now: now)
    let snapshot = await risk.snapshot(now: now)
    #expect(snapshot.equity == Decimal(9_500))
    #expect(snapshot.dailyPnLPercent == Decimal(-5))
    #expect(snapshot.killSwitch)
    #expect(snapshot.reason == "单日亏损熔断")
}

@Test
func riskEngineTripwiresCumulativeDrawdownLimit() async {
    let risk = RiskEngine(initialEquity: 10_000)
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(secondsFromGMT: 0)!
    let dayOne = calendar.date(from: DateComponents(year: 2026, month: 9, day: 1, hour: 12))!
    let dayTwo = calendar.date(from: DateComponents(year: 2026, month: 9, day: 2, hour: 12))!

    await risk.record(realizedPnL: 2_000, now: dayOne)
    await risk.synchronizeEquity(11_000, now: dayTwo)
    await risk.markToMarket(unrealizedPnL: -500, now: dayTwo)

    let snapshot = await risk.snapshot(now: dayTwo)
    #expect(snapshot.drawdownPercent > 10)
    #expect(snapshot.dailyPnLPercent > -5)
    #expect(snapshot.killSwitch)
    #expect(snapshot.reason == "累计回撤熔断")
}

@Test
func strategyCapitalPoolsAreIsolatedAndCompoundRealizedPnL() async {
    let risk = RiskEngine(limits: RiskLimits(maxMarginPercent: 100, minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let first = UUID()
    let second = UUID()
    let now = Date(timeIntervalSince1970: 1_700_000_000)
    let firstPool = await risk.registerStrategy(first, allocationPercent: 50, now: now)
    let secondPool = await risk.registerStrategy(second, allocationPercent: 50, now: now)
    #expect(firstPool.equity == Decimal(5_000))
    #expect(secondPool.equity == Decimal(5_000))
    let allowed = await risk.authorize(instrumentID: "A-USDT-SWAP", notional: 4_000, margin: 4_000, now: now, strategyID: first, poolAllocationPercent: 50)
    let rejected = await risk.authorize(instrumentID: "B-USDT-SWAP", notional: 2_000, margin: 2_000, now: now.addingTimeInterval(1), strategyID: first, poolAllocationPercent: 50)
    #expect(allowed.allowed)
    #expect(!rejected.allowed)
    await risk.release(instrumentID: "A-USDT-SWAP", notional: 4_000, strategyID: first, margin: 4_000)
    await risk.record(realizedPnL: 500, now: now.addingTimeInterval(2), strategyID: first)
    let compounded = await risk.strategyCapital(first, allocationPercent: 50, now: now.addingTimeInterval(2))
    let isolated = await risk.strategyCapital(second, allocationPercent: 50, now: now.addingTimeInterval(2))
    #expect(compounded.equity == Decimal(5_500))
    #expect(compounded.realizedPnL == Decimal(500))
    #expect(compounded.rolloverCount == 1)
    #expect(isolated.equity == Decimal(5_000))
}

@Test
func remoteStrategyRealizedCreditOnlyChangesItsPoolUntilAccountReconciliation() async {
    let risk = RiskEngine(initialEquity: 10_000)
    let strategyID = UUID()
    _ = await risk.registerStrategy(strategyID, allocationPercent: 100)

    await risk.recordStrategyRealized(125, strategyID: strategyID)

    let pool = await risk.strategyCapital(strategyID)
    #expect(pool.equity == Decimal(10_125))
    #expect(pool.realizedPnL == Decimal(125))
    #expect(pool.rolloverCount == 1)
    #expect((await risk.snapshot()).equity == Decimal(10_000))
}

@Test
func riskEngineRestoreDoesNotCarryStalePoolMarkAfterRestart() async {
    let strategyID = UUID()
    let now = Date(timeIntervalSince1970: 1_700_000_000)
    let persisted = RiskSnapshot(
        equity: 10_000,
        equityPeak: 10_000,
        dayStartEquity: 10_000,
        dayStartAt: now,
        strategyCapitals: [
            StrategyCapitalSnapshot(
                strategyID: strategyID,
                allocationPercent: 50,
                initialCapital: 5_000,
                equity: 5_000,
                reservedCapital: 125,
                unrealizedPnL: 275,
                openRisk: 12,
                openPositions: 1
            )
        ]
    )
    let risk = RiskEngine(initialEquity: 1)
    await risk.restore(persisted, now: now)

    let pool = await risk.strategyCapital(strategyID)
    #expect(pool.equity == Decimal(5_000))
    #expect(pool.unrealizedPnL == 0)
    #expect(pool.reservedCapital == Decimal(125))
    #expect(pool.openRisk == Decimal(12))
    #expect(pool.openPositions == 1)
    #expect(pool.availableCapital == Decimal(4_875))
}

@Test
func riskEngineFirstAccountSyncUsesRealEquityAsBaseline() async {
    let risk = RiskEngine(initialEquity: 100_000)
    let now = Date(timeIntervalSince1970: 1_700_000_000)
    await risk.synchronizeEquity(1_000, now: now)
    let snapshot = await risk.snapshot(now: now)
    #expect(snapshot.equity == Decimal(1_000))
    #expect(snapshot.dayStartEquity == Decimal(1_000))
    #expect(snapshot.dailyPnLPercent == 0)
    #expect(!snapshot.killSwitch)
    let decision = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 260, margin: 260, now: now)
    #expect(!decision.allowed)
    #expect(decision.reason == "单笔保证金超过权益比例")
}

@Test
func riskEngineRejectsNewOrdersAfterAuthenticatedEquityReachesZero() async {
    let risk = RiskEngine(initialEquity: 100_000)
    await risk.synchronizeEquity(0)

    let snapshot = await risk.snapshot()
    #expect(snapshot.equity == 0)
    #expect(snapshot.killSwitch)
    #expect(snapshot.reason == "账户权益为零")

    let entry = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 100, margin: 10)
    #expect(!entry.allowed)
    #expect(entry.reason == "账户权益为零")

    // Reduce-only exits remain available so an empty account can still close
    // a stale remote position during cleanup.
    let exit = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 100, margin: 10, reduceOnly: true)
    #expect(exit.allowed)
}

@Test
func riskEngineRestorePreservesAuthenticatedZeroEquityCircuitBreaker() async {
    let now = Date(timeIntervalSince1970: 1_700_000_000)
    let persisted = RiskSnapshot(
        equity: 0,
        equityPeak: 0,
        dayStartEquity: 0,
        dayStartAt: now,
        killSwitch: false,
        reason: nil,
        strategyCapitalBase: 0
    )
    let risk = RiskEngine(initialEquity: 100_000)
    await risk.restore(persisted, now: now)

    let snapshot = await risk.snapshot(now: now)
    #expect(snapshot.equity == 0)
    #expect(snapshot.killSwitch)
    #expect(snapshot.reason == "账户权益为零")

    let entry = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 100, margin: 10, now: now)
    #expect(!entry.allowed)
    #expect(entry.reason == "账户权益为零")

    let exit = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 100, margin: 10, reduceOnly: true, now: now)
    #expect(exit.allowed)
}

@Test
func strategyPoolsUseUSDTBaseWhileGlobalEquityStaysAllAsset() async {
    let risk = RiskEngine(initialEquity: 100_000)
    let strategyID = UUID()
    _ = await risk.registerStrategy(strategyID, allocationPercent: 50)

    await risk.synchronizeEquity(100_000)
    await risk.synchronizeStrategyCapital(20_000)

    let pool = await risk.strategyCapital(strategyID)
    let snapshot = await risk.snapshot()
    #expect(pool.initialCapital == Decimal(10_000))
    #expect(pool.equity == Decimal(10_000))
    #expect(snapshot.equity == Decimal(100_000))
    #expect(snapshot.strategyCapitalBase == Decimal(20_000))
}

@Test
func zeroUSDTBaseDoesNotFallBackToAccountEquity() async {
    let risk = RiskEngine(initialEquity: 100_000)
    let first = UUID()
    let second = UUID()
    _ = await risk.registerStrategy(first, allocationPercent: 50)

    await risk.synchronizeStrategyCapital(0)
    _ = await risk.registerStrategy(second, allocationPercent: 100)

    let pools = await risk.strategyCapitals()
    #expect(pools.allSatisfy { $0.equity == 0 && $0.initialCapital == 0 })
    #expect((await risk.snapshot()).equity == Decimal(100_000))
    #expect((await risk.snapshot()).strategyCapitalBase == 0)
}

@Test
func strategyPoolShrinksToNewUSDTAllocationWithOpenReservation() async {
    let risk = RiskEngine(limits: RiskLimits(maxMarginPercent: 100, minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let strategyID = UUID()
    await risk.synchronizeStrategyCapital(10_000)
    _ = await risk.registerStrategy(strategyID, allocationPercent: 100)

    let entry = await risk.authorize(
        instrumentID: "ALT-USDT-SWAP", notional: 4_000, margin: 4_000,
        strategyID: strategyID, poolAllocationPercent: 100
    )
    #expect(entry.allowed)

    await risk.synchronizeStrategyCapital(5_000)
    let pool = await risk.strategyCapital(strategyID)
    #expect(pool.equity == Decimal(5_000))
    #expect(pool.reservedCapital == Decimal(4_000))
    #expect(pool.availableCapital == Decimal(1_000))

    let oversized = await risk.authorize(
        instrumentID: "OTHER-USDT-SWAP", notional: 1_001, margin: 1_001,
        strategyID: strategyID, poolAllocationPercent: 100
    )
    #expect(!oversized.allowed)
    #expect(oversized.reason == "策略资金池可用余额不足")
}

@Test
func existingStrategyAllocationUpdateResizesRuntimePool() async {
    let risk = RiskEngine(initialEquity: 10_000)
    await risk.synchronizeStrategyCapital(10_000)
    let strategyID = UUID()
    let initial = await risk.registerStrategy(strategyID, allocationPercent: 50)
    #expect(initial.equity == Decimal(5_000))

    let updated = await risk.updateStrategyAllocation(strategyID, allocationPercent: 80)
    #expect(updated.allocationPercent == Decimal(80))
    #expect(updated.initialCapital == Decimal(8_000))
    #expect(updated.equity == Decimal(8_000))
}

@Test
func newStrategyAllocationUsesRemainingUSDTPoolEquity() async {
    let risk = RiskEngine(initialEquity: 100_000)
    await risk.synchronizeStrategyCapital(10_000)
    let first = await risk.registerStrategy(UUID(), allocationPercent: 60)
    let second = await risk.registerStrategy(UUID(), allocationPercent: 60)

    #expect(first.allocationPercent == Decimal(60))
    #expect(first.equity == Decimal(6_000))
    #expect(second.allocationPercent == Decimal(40))
    #expect(second.equity == Decimal(4_000))
}

@Test
func firstCapitalSyncUsesUSDTAndCapsPoolsToCurrentBalance() async {
    let firstID = UUID()
    let secondID = UUID()
    let now = Date(timeIntervalSince1970: 1_700_000_000)
    let persisted = RiskSnapshot(
        equity: 100_000,
        equityPeak: 100_000,
        dayStartEquity: 100_000,
        dayStartAt: now,
        strategyCapitals: [
            StrategyCapitalSnapshot(strategyID: firstID, allocationPercent: 50,
                                     initialCapital: 50_000, equity: 51_000,
                                     reservedCapital: 2_000, realizedPnL: 1_000,
                                     openRisk: 100, openPositions: 1),
            StrategyCapitalSnapshot(strategyID: secondID, allocationPercent: 50,
                                     initialCapital: 50_000, equity: 50_000)
        ]
    )
    let risk = RiskEngine(initialEquity: 1)
    await risk.restore(persisted, now: now)
    await risk.synchronizeStrategyCapital(20_000, now: now)

    let first = await risk.strategyCapital(firstID)
    let second = await risk.strategyCapital(secondID)
    #expect(first.initialCapital == Decimal(10_000))
    #expect(first.equity == Decimal(10_000))
    #expect(first.reservedCapital == Decimal(2_000))
    #expect(first.openRisk == Decimal(100))
    #expect(first.openPositions == 1)
    #expect(second.initialCapital == Decimal(10_000))
    #expect(second.equity == Decimal(10_000))

    await risk.synchronizeStrategyCapital(10_000, now: now.addingTimeInterval(1))
    let resizedFirst = await risk.strategyCapital(firstID)
    let resizedSecond = await risk.strategyCapital(secondID)
    #expect(resizedFirst.equity == Decimal(5_000))
    #expect(resizedSecond.equity == Decimal(5_000))
}

@Test
func paperBrokerPartialCloseKeepsRemainderAndMergesAdditions() async {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let base = Date(timeIntervalSince1970: 1_700_000_000)

    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 2, referencePrice: 100, requestedAt: base)
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(60), open: 100, high: 101, low: 99, close: 100))
    var positions = await broker.allPositions()
    #expect(positions.count == 1)
    #expect(positions[0].quantity == 2)

    // A closing order smaller than the position must not wipe the remainder.
    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "short", quantity: 1, referencePrice: 110, requestedAt: base.addingTimeInterval(120))
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(180), open: 110, high: 111, low: 109, close: 110))
    positions = await broker.allPositions()
    #expect(positions.count == 1)
    #expect(positions[0].side == "long")
    #expect(positions[0].quantity == 1)
    #expect(positions[0].entryPrice == 100)

    // Adding to the same side merges the average entry price.
    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 1, referencePrice: 120, requestedAt: base.addingTimeInterval(240))
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(300), open: 120, high: 121, low: 119, close: 120))
    positions = await broker.allPositions()
    #expect(positions.count == 1)
    #expect(positions[0].quantity == 2)
    #expect(positions[0].entryPrice == 110)

    // The close realized +10 and the remaining two-lot position is marked at
    // 120, so account equity includes its +20 unrealized PnL.
    let snapshot = await risk.snapshot(now: base.addingTimeInterval(300))
    #expect(snapshot.equity == Decimal(10_030))
}

@Test
func paperBrokerRealizedCloseDoesNotDoubleCountPriorMark() async {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let base = Date(timeIntervalSince1970: 1_700_100_000)
    let strategyID = UUID()

    _ = await broker.submit(strategyID: strategyID, instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 1, referencePrice: 100, requestedAt: base)
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(60), open: 100, high: 101, low: 99, close: 100))
    await broker.mark(instrumentID: "BTC-USDT-SWAP", price: 90, at: base.addingTimeInterval(120))

    _ = await broker.submit(strategyID: strategyID, instrumentID: "BTC-USDT-SWAP", side: "short", quantity: 1, referencePrice: 90, requestedAt: base.addingTimeInterval(180))
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(240), open: 90, high: 91, low: 89, close: 90))

    // The -10 mark is crystallized once by the close; it must not be added a
    // second time on top of the existing mark-to-market equity.
    #expect((await risk.snapshot(now: base.addingTimeInterval(240))).equity == Decimal(9_990))
}

@Test
func paperBrokerSameSideAdditionChargesFeeToAccountAndStrategyPool() async {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0.001, slippageBps: 0)
    let strategyID = UUID()
    let base = Date(timeIntervalSince1970: 1_700_110_000)
    _ = await risk.registerStrategy(strategyID, allocationPercent: 100, now: base)

    _ = await broker.submit(strategyID: strategyID, instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 1, referencePrice: 100, requestedAt: base)
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(60), open: 100, high: 101, low: 99, close: 100))
    _ = await broker.submit(strategyID: strategyID, instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 1, referencePrice: 120, requestedAt: base.addingTimeInterval(120))
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(180), open: 120, high: 121, low: 119, close: 120))

    // Entry fees are 0.10 and 0.12. The two-lot position is marked at 120
    // against a blended 110 entry, so equity is 10,000 + 20 - 0.22.
    let snapshot = await risk.snapshot(now: base.addingTimeInterval(180))
    #expect(snapshot.equity == Decimal(string: "10019.78"))
    let pool = await risk.strategyCapital(strategyID, now: base.addingTimeInterval(180))
    #expect(pool.equity == Decimal(string: "9999.78"))
    #expect(pool.realizedPnL == Decimal(string: "-0.22"))
}

@Test
func paperBrokerReduceOnlyCannotOpenOrReverse() async {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let base = Date(timeIntervalSince1970: 1_700_000_000)

    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "short", quantity: 1, referencePrice: 100, requestedAt: base, reduceOnly: true)
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(60), open: 100, high: 101, low: 99, close: 100))
    #expect((await broker.allPositions()).isEmpty)
    #expect((await broker.allFills()).isEmpty)

    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 2, referencePrice: 100, requestedAt: base.addingTimeInterval(120))
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(180), open: 100, high: 101, low: 99, close: 100))
    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "short", quantity: 3, referencePrice: 100, requestedAt: base.addingTimeInterval(240), reduceOnly: true)
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(300), open: 100, high: 101, low: 99, close: 100))
    #expect((await broker.allPositions()).isEmpty)
    #expect((await broker.allFills()).last?.quantity == 2)
}

@Test
func paperBrokerOversizedReverseKeepsResidualPosition() async {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let base = Date(timeIntervalSince1970: 1_700_000_000)

    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 1, referencePrice: 100, requestedAt: base)
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(60), open: 100, high: 101, low: 99, close: 100))
    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "short", quantity: 2, referencePrice: 110, requestedAt: base.addingTimeInterval(120))
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(180), open: 110, high: 111, low: 109, close: 110))
    let positions = await broker.allPositions()
    #expect(positions.count == 1)
    #expect(positions[0].side == "short")
    #expect(positions[0].quantity == 1)
    #expect(positions[0].entryPrice == 110)
    #expect((await risk.snapshot()).equity == Decimal(10_010))
}

@Test
func paperBrokerNormalCloseReleasesBothRiskReservations() async {
    let risk = RiskEngine(limits: RiskLimits(maxInstrumentNotional: 300, maxMarginPercent: 100, minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let broker = PaperBroker(risk: risk, feeRate: 0, slippageBps: 0)
    let base = Date(timeIntervalSince1970: 1_700_000_000)

    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 1, referencePrice: 100, requestedAt: base)
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(60), open: 100, high: 101, low: 99, close: 100))
    _ = await broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "short", quantity: 1, referencePrice: 110, requestedAt: base.addingTimeInterval(120))
    await broker.processNextOpen(instrumentID: "BTC-USDT-SWAP", candle: Candle(timestamp: base.addingTimeInterval(180), open: 110, high: 111, low: 109, close: 110))

    // Both the old position reservation (100) and the closing order
    // reservation (110) are gone after an exact normal close.
    let decision = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 300, margin: 300, now: base.addingTimeInterval(240))
    #expect(decision.allowed)
}

@Test
func realtimeUnconfirmedBarFillsPendingOrderAtItsOpen() async {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let stateDirectory = FileManager.default.temporaryDirectory.appendingPathComponent("NovaTrade-test-" + UUID().uuidString, isDirectory: true)
    let backend = TradingBackend(paper: PaperTradingStore(directory: stateDirectory), riskEngine: risk)
    let signalTime = Date(timeIntervalSince1970: 1_700_000_000)
    _ = await backend.broker.submit(strategyID: UUID(), instrumentID: "BTC-USDT-SWAP", side: "long", quantity: 1, referencePrice: 100, requestedAt: signalTime)

    _ = await backend.ingestRealtimeCandle(Candle(timestamp: signalTime.addingTimeInterval(60), open: 100, high: 112, low: 99, close: 110, confirmed: false), instrumentID: "BTC-USDT-SWAP", interval: .oneMinute)
    let positions = await backend.broker.allPositions()
    #expect(positions.count == 1)
    #expect(positions[0].entryPrice == Decimal(string: "100.02"))
    #expect(positions[0].markPrice == 110)
    #expect(positions[0].unrealizedPnL == Decimal(string: "9.98"))
    try? FileManager.default.removeItem(at: stateDirectory)
}

@Test
func candleUpsertKeepsTimeOrderAndReplacesInPlace() {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    var candles: [Candle] = []
    candles.upsert(Candle(timestamp: start, open: 1, high: 2, low: 0.5, close: 1.5, confirmed: false))
    candles.upsert(Candle(timestamp: start.addingTimeInterval(60), open: 2, high: 3, low: 1.5, close: 2.5))
    #expect(candles.count == 2)
    candles.upsert(Candle(timestamp: start, open: 1, high: 9, low: 0.5, close: 1.5, confirmed: true))
    #expect(candles.count == 2)
    #expect(candles[0].high == 9)
    #expect(candles[0].confirmed)
    // An out-of-order older bar lands at the front without a full re-sort.
    candles.upsert(Candle(timestamp: start.addingTimeInterval(-60), open: 0, high: 1, low: 0, close: 1))
    #expect(candles.count == 3)
    #expect(candles[0].timestamp < candles[1].timestamp)
}

@Test
func runtimeLogsPersistAcrossBackendRestart() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-runtime-log-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }

    let first = TradingBackend(paper: PaperTradingStore(directory: directory))
    await first.appendLog("持久化策略日志", level: "signal")

    let second = TradingBackend(paper: PaperTradingStore(directory: directory))
    let logs = await second.logs()
    #expect(logs.contains { $0.level == "signal" && $0.message == "持久化策略日志" })
}
