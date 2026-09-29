import Foundation
import Testing
import TradingDomain
@testable import TradingService

/// 双均线交易山寨币多（1h）策略引擎测试。
/// 实验室规则：`strategies/ema_altcoin_long/STRATEGY.md`（v1.0.0）与 §8 运行时映射契约。
/// 构造序列：700 根匀速上行（多头排列 + 均线密集 + ATR/价格 ≈ 0.5%）→ 突破 K 线
/// → 首次回踩 EMA20 且收盘仍在其上方（最后一根）。

private func makeEmaBars(warmup: Int = 700, drift: Double = 0.005) -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    return (0..<warmup).map { index in
        let close = 100 + drift * Double(index)
        return Candle(timestamp: start.addingTimeInterval(Double(index) * 3600),
                      open: Decimal(close), high: Decimal(close + 0.25), low: Decimal(close - 0.25),
                      close: Decimal(close), volume: 1_000, confirmed: true)
    }
}

private func bar(_ index: Int, open: Double, high: Double, low: Double, close: Double, confirmed: Bool = true) -> Candle {
    Candle(timestamp: Date(timeIntervalSince1970: 1_700_000_000 + Double(index) * 3600),
           open: Decimal(open), high: Decimal(high), low: Decimal(low), close: Decimal(close),
           volume: 1_000, confirmed: confirmed)
}

/// 突破 + 首次回踩，最后一根就是回踩确认 K 线。
private func makeEmaPullbackCandles() -> [Candle] {
    var candles = makeEmaBars()
    let warmup = candles.count
    let closes = candles.map { NSDecimalNumber(decimal: $0.close).doubleValue }
    let atr = IndicatorCalculator.atrEMA(candles)
    let priorHigh = candles[(warmup - 4)..<warmup].map { NSDecimalNumber(decimal: $0.high).doubleValue }.max() ?? closes[warmup - 1]

    let breakoutClose = priorHigh + 0.45 * atr[warmup - 1]
    candles.append(bar(warmup, open: closes[warmup - 1], high: breakoutClose + 0.05,
                       low: closes[warmup - 1] - 0.01, close: breakoutClose))

    let closesAfter = candles.map { NSDecimalNumber(decimal: $0.close).doubleValue }
    let fastAfter = IndicatorCalculator.ema(closesAfter, period: 20)
    let atrAfter = IndicatorCalculator.atrEMA(candles)
    let index = warmup
    let touch = fastAfter[index] + 0.35 * atrAfter[index]
    candles.append(bar(warmup + 1, open: fastAfter[index] + 0.06, high: fastAfter[index] + 0.10,
                       low: touch - 0.02, close: fastAfter[index] + 0.05))
    return candles
}

/// BTC 1h：持续上行 → close > EMA60 且 EMA60 > 六小时前的 EMA60（门控开）。
private func makeBullishBTC() -> [Candle] {
    makeEmaBars(warmup: 300, drift: 0.02)
}

private func makeEmaConfig(riskPercent: Double = 0.5) -> StrategyConfig {
    StrategyConfig(name: "双均线交易山寨币多（1h）", scope: .dynamic(.hotAltcoins), interval: .oneHour,
                   type: .emaAltcoinLong, parameters: StrategyType.emaAltcoinLong.defaultParameters,
                   enabled: true, riskPercent: riskPercent, capitalPoolPercent: 100, cooldownBars: 96)
}

@Test
func emaAltcoinLongEmitsLongSignalOnBreakoutPullback() {
    let engine = StrategyEngine()
    let candles = makeEmaPullbackCandles()
    let status = engine.evaluateEmaAltcoinLong(config: makeEmaConfig(), candles: candles, btcCandles: makeBullishBTC())
    let signal = status.lastSignal
    #expect(signal != nil)
    #expect(signal?.type == "entry_long")
    #expect(status.direction == "long")

    let closes = candles.map { NSDecimalNumber(decimal: $0.close).doubleValue }
    let atr = IndicatorCalculator.atrEMA(candles)
    let index = candles.count - 1
    let entry = closes[index]
    let risk = 1.25 * atr[index]
    #expect(abs(NSDecimalNumber(decimal: signal!.price).doubleValue - entry) < 1e-9)
    #expect(abs(NSDecimalNumber(decimal: signal!.stopPrice ?? 0).doubleValue - (entry - risk)) < 1e-9)
    #expect(abs(NSDecimalNumber(decimal: signal!.takePrice ?? 0).doubleValue - (entry + 2.5 * risk)) < 1e-9)
    // 信号时刻是回踩确认 K 线本身。
    #expect(signal?.timestamp == candles[index].timestamp)
}

@Test
func emaAltcoinLongNeedsBtcGateHistory() {
    let engine = StrategyEngine()
    let candles = makeEmaPullbackCandles()
    // 没有 BTC K 线 → 门控 fail-closed。
    #expect(engine.evaluateEmaAltcoinLong(config: makeEmaConfig(), candles: candles).lastSignal == nil)
    // BTC 历史不足 120 根 → 同样不发信号。
    let shortBTC = Array(makeBullishBTC().prefix(60))
    #expect(engine.evaluateEmaAltcoinLong(config: makeEmaConfig(), candles: candles, btcCandles: shortBTC).lastSignal == nil)
}

@Test
func emaAltcoinLongSkipsHoursWithGaps() {
    let engine = StrategyEngine()
    var candles = makeEmaPullbackCandles()
    let last = candles.removeLast()
    // 插入一根错位的 K 线，使最后一根与前一根相隔 2 小时。
    candles.append(bar(candles.count + 1, open: 0, high: 0, low: 0, close: 0))
    candles.append(Candle(timestamp: last.timestamp.addingTimeInterval(3600), open: last.open,
                          high: last.high, low: last.low, close: last.close, volume: 1_000, confirmed: true))
    let status = engine.evaluateEmaAltcoinLong(config: makeEmaConfig(), candles: candles, btcCandles: makeBullishBTC())
    #expect(status.lastSignal == nil)
}

@Test
func emaAltcoinLongIsIdempotentPerBar() {
    let engine = StrategyEngine()
    let candles = makeEmaPullbackCandles()
    let btc = makeBullishBTC()
    let first = engine.evaluateEmaAltcoinLong(config: makeEmaConfig(), candles: candles, btcCandles: btc)
    #expect(first.lastSignal != nil)
    #expect(first.cooldown == 96)

    var repeated = first
    for _ in 0..<5 {
        repeated = engine.evaluateEmaAltcoinLong(config: makeEmaConfig(), candles: candles, previous: repeated, btcCandles: btc)
    }
    #expect(repeated.lastSignal?.id == first.lastSignal?.id)
    #expect(repeated.cooldown == 96)
}

@Test
func strategyStoreAcceptsEmaAltcoinLongAndClampsRisk() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-ema-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = PaperTradingStore(directory: directory)

    // 传入 1.0% 会被实验室的 0.5% 上限收敛。
    let created = try await store.create(makeEmaConfig(riskPercent: 1.0))
    #expect(created.riskPercent == 0.5)
    #expect(created.scope == .dynamic(.hotAltcoins))
    #expect(created.interval == .oneHour)
    #expect(created.parameters["emaFast"] == 20)
    #expect(created.parameters["maxConcurrentPositions"] == 6)
    #expect(created.name == "双均线交易山寨币多（1h）")
    #expect((await store.allStrategies()).count == 1)
}

/// 资金池必须按实验室的组合上限执行：开放止损风险 ≤ 池权益 3%（= 6 并发 × 每笔 0.5%）。
@Test
func riskEngineEnforcesStrategyOpenRiskAndConcurrencyCaps() async {
    let risk = RiskEngine(limits: RiskLimits(minOrderIntervalSeconds: 0), initialEquity: 10_000)
    let strategy = UUID()
    _ = await risk.registerStrategy(strategy, allocationPercent: 100, now: Date(timeIntervalSince1970: 1_700_000_000))

    // 每笔风险 50U（池权益 10_000 的 0.5%），上限 300U → 只能通过 6 笔。
    var allowed = 0
    var lastReason: String?
    for index in 0..<7 {
        let decision = await risk.authorize(
            instrumentID: "ALT\(index)-USDT-SWAP", notional: 1_000, margin: 1_000,
            now: Date(timeIntervalSince1970: 1_700_000_000 + Double(index) * 60),
            strategyID: strategy, poolAllocationPercent: 100,
            riskAmount: 50, maxOpenRiskPercent: 3.0, maxConcurrentPositions: 6
        )
        if decision.allowed { allowed += 1 } else { lastReason = decision.reason }
    }
    #expect(allowed == 6)
    #expect(lastReason == "策略开放止损风险超过上限")

    var pool = await risk.strategyCapital(strategy, allocationPercent: 100)
    #expect(pool.openPositions == 6)
    #expect(pool.openRisk == 300)

    // 平掉第一笔后，开放风险与并发名额同时释放，可以再开一笔。
    await risk.release(instrumentID: "ALT0-USDT-SWAP", notional: 1_000, strategyID: strategy,
                       margin: 1_000, riskAmount: 50, closedPosition: true)
    pool = await risk.strategyCapital(strategy, allocationPercent: 100)
    #expect(pool.openPositions == 5)
    #expect(pool.openRisk == 250)

    let reopened = await risk.authorize(
        instrumentID: "ALT7-USDT-SWAP", notional: 1_000, margin: 1_000,
        now: Date(timeIntervalSince1970: 1_700_000_000 + 600), strategyID: strategy,
        poolAllocationPercent: 100, riskAmount: 50, maxOpenRiskPercent: 3.0, maxConcurrentPositions: 6
    )
    #expect(reopened.allowed)
    pool = await risk.strategyCapital(strategy, allocationPercent: 100)
    #expect(pool.openPositions == 6)
    #expect(pool.openRisk == 300)
}
