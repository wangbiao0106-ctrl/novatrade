import Foundation
import Testing
import TradingDomain
@testable import TradingService

@Test
func strategyScopesResolveSingleMultipleAndDynamicTargets() throws {
    let contracts = [
        ContractMarket(id: "BTC-USDT-SWAP", name: "BTC", baseCurrency: "BTC", quoteCurrency: "USDT", last: 100, changePercent: 2, volume24h: 1_000_000_000),
        ContractMarket(id: "ALT-USDT-SWAP", name: "ALT", baseCurrency: "ALT", quoteCurrency: "USDT", last: 1, changePercent: 60, volume24h: 50_000_000),
        ContractMarket(id: "MOON-USDT-SWAP", name: "MOON", baseCurrency: "MOON", quoteCurrency: "USDT", last: 1, changePercent: 101, volume24h: 40_000_000)
    ]
    #expect(StrategyScope.single("BTC-USDT-SWAP").matches("BTC-USDT-SWAP", contracts: contracts))
    #expect(StrategyScope.multiple(["BTC-USDT-SWAP", "ALT-USDT-SWAP"]).resolvedInstrumentIDs(from: contracts).count == 2)
    #expect(StrategyScope.dynamic(.highGain60).resolvedInstrumentIDs(from: contracts) == ["MOON-USDT-SWAP", "ALT-USDT-SWAP"])
    #expect(StrategyScope.dynamic(.highGain100).resolvedInstrumentIDs(from: contracts) == ["MOON-USDT-SWAP"])
}

@Test
func legacyStrategyConfigDefaultsToSingleScope() throws {
    let data = Data(#"{"id":"00000000-0000-0000-0000-000000000001","name":"legacy","instrumentID":"BTC-USDT-SWAP","interval":"1H","type":"trendFollowing","parameters":{},"enabled":false,"stopLossPercent":1.5,"takeProfitPercent":3,"riskPercent":1,"cooldownBars":3,"trailingStopPercent":1}"#.utf8)
    let config = try JSONDecoder().decode(StrategyConfig.self, from: data)
    #expect(config.scope == nil)
    #expect(config.effectiveScope.matches("BTC-USDT-SWAP", contracts: []))
}

@Test
func strategyStoreAllowsOneInstancePerRuleAndDeletesIt() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-strategy-store-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = PaperTradingStore(directory: directory)
    #expect((await store.allStrategies()).isEmpty)

    let config = StrategyConfig(name: "唯一趋势", instrumentID: "BTC-USDT-SWAP", interval: .fifteenMinutes, type: .trendFollowing)
    _ = try await store.create(config)
    do {
        _ = try await store.create(StrategyConfig(name: "重复趋势", instrumentID: "ETH-USDT-SWAP", interval: .fifteenMinutes, type: .trendFollowing))
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
func riskEngineRollsDailyBaselineWhenCalendarDayChanges() async {
    // Same calendar convention as RiskEngine: gregorian in the local time zone.
    let calendar = Calendar(identifier: .gregorian)
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

    // The close realized +10 with zero fee/slippage in this setup.
    let snapshot = await risk.snapshot(now: base.addingTimeInterval(300))
    #expect(snapshot.equity == Decimal(10_010))
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
    let positions = await backend.positions()
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
func strategyStatusIndicatorSeriesStayBounded() {
    let engine = StrategyEngine()
    let config = StrategyConfig(name: "趋势", instrumentID: "BTC-USDT-SWAP", interval: .oneHour, type: .trendFollowing, enabled: true)
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    var candles: [Candle] = []
    var price = 100.0
    for index in 0..<300 {
        price += Double((index % 7) - 3) * 0.1
        candles.append(Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: Decimal(price), high: Decimal(price + 1), low: Decimal(price - 1), close: Decimal(price), volume: 10, confirmed: true))
    }
    let status = engine.evaluate(config: config, candles: candles)
    #expect(status.indicators["emaFast"]?.count == 120)
    #expect(status.indicators["emaSlow"]?.count == 120)
    #expect(status.indicators["rsi"]?.count == 120)
    #expect(status.indicators["atr"]?.count == 120)
}

@Test
func strategyClampsInvalidIndicatorPeriods() {
    let engine = StrategyEngine()
    let config = StrategyConfig(name: "边界", instrumentID: "BTC-USDT-SWAP", interval: .oneHour, type: .trendFollowing, parameters: ["fastEMA": 0, "slowEMA": Double.greatestFiniteMagnitude, "atrPeriod": 0, "donchianPeriod": 0], enabled: true)
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    let candles = (0..<3).map { index in
        Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: 100, high: 101, low: 99, close: 100, volume: 1)
    }
    let status = engine.evaluate(config: config, candles: candles)
    #expect(status.indicators["emaFast"]?.count == 3)
    #expect(status.indicators["emaSlow"]?.count == 3)
    #expect(status.indicators["atr"]?.count == 3)
}

@Test
func strategySignalUsesCandleTimeAndIsStableForRepeatedBar() {
    let engine = StrategyEngine()
    let config = StrategyConfig(name: "RSI", instrumentID: "BTC-USDT-SWAP", interval: .oneMinute, type: .rsiReversal, parameters: ["period": 2, "oversold": 30, "overbought": 70], enabled: true)
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    let candles = [
        Candle(timestamp: start, open: 100, high: 101, low: 99, close: 100),
        Candle(timestamp: start.addingTimeInterval(60), open: 90, high: 91, low: 89, close: 90),
        Candle(timestamp: start.addingTimeInterval(120), open: 95, high: 96, low: 94, close: 95)
    ]
    let first = engine.evaluate(config: config, candles: candles)
    let second = engine.evaluate(config: config, candles: candles, previous: first)
    #expect(first.lastSignal?.timestamp == candles.last?.timestamp)
    #expect(second.lastSignal?.id == first.lastSignal?.id)
}
