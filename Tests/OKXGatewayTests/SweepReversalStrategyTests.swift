import Foundation
import Testing
import TradingDomain
@testable import TradingService

/// 山寨币二次扫顶做空策略引擎测试：与 Python 回测（strategies/sweep_reversal_short/engine.py）逐条对应。
/// 构造序列：完整 288 根暖机 → 高位摆动点 p → 首次扫顶 s（放量、收盘回落）→ 二次扫顶 j（更低高点、收盘回落，最后一根）。

private func makeSweepCandles(resweep: Bool = true) -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    var candles: [Candle] = []
    var price = 90.0
    for index in 0..<319 {
        var high: Double
        var low: Double
        var close: Double
        var volume: Double = 100
        switch index {
        case 0..<300:                    // 缓涨 + 完整 288 根高位窗口
            price += 0.02
            close = price
            high = close + 0.3
            low = close - 0.3
        case 300:                        // 摆动高点 p：288 根最高
            price += 0.043
            close = 102.0
            high = 103.0
            low = 101.5
        case 301...305:                  // 右臂（不高于 p）
            price = 101.6
            close = price
            high = 102.4
            low = 101.2
        case 306:                        // 首次扫顶 s：放量、收盘回落
            close = 102.5
            high = 104.0
            low = 101.9
            volume = 200
        case 307...317:                  // 中间整理
            close = 101.5
            high = 102.0
            low = 101.1
        case 318:                        // 二次扫顶 j（最后一根）
            if resweep {
                close = 102.4
                high = 103.5
                low = 101.9
            } else {
                close = 101.5
                high = 102.2
                low = 101.1
            }
        default:
            close = price
            high = close + 0.3
            low = close - 0.3
        }
        candles.append(Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: Decimal(close - 0.05), high: Decimal(high), low: Decimal(low), close: Decimal(close), volume: Decimal(volume), confirmed: true))
    }
    return candles
}

/// BTC 1H：前高后低 → 收盘低于 SMA200（门控开）
private func makeBTCBearishCandles() -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    return (0..<360).map { index in
        let close = 200.0 - Double(index) * 0.42   // 200 → 100
        return Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: Decimal(close), high: Decimal(close + 1), low: Decimal(close - 1), close: Decimal(close), volume: 100, confirmed: true)
    }
}

/// BTC 1H：前低后高 → 收盘高于 SMA200（门控关）
private func makeBTCBullishCandles() -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    return (0..<360).map { index in
        let close = 100.0 + Double(index) * 0.42   // 100 → 200
        return Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: Decimal(close), high: Decimal(close + 1), low: Decimal(close - 1), close: Decimal(close), volume: 100, confirmed: true)
    }
}

/// The total fixture is long enough to contain a 200-bar history, but the
/// target signal arrives before the 200th confirmed BTC bar. The gate must use
/// only bars at or before the signal timestamp and therefore remain closed.
private func makeBTCWithInsufficientHistoryAtSignal() -> [Candle] {
    let signalTimestamp = Date(timeIntervalSince1970: 1_700_000_000 + 318 * 3600)
    let start = signalTimestamp.addingTimeInterval(-150 * 3600)
    return (0..<360).map { index in
        let close = 200.0 - Double(index) * 0.42
        return Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: Decimal(close), high: Decimal(close + 1), low: Decimal(close - 1), close: Decimal(close), volume: 100, confirmed: true)
    }
}

private func makeBTCBearishCandlesWithPriorTimestamp() -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000).addingTimeInterval(-1800)
    return (0..<360).map { index in
        let close = 200.0 - Double(index) * 0.42
        return Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: Decimal(close), high: Decimal(close + 1), low: Decimal(close - 1), close: Decimal(close), volume: 100, confirmed: true)
    }
}

private func makeSweepConfig() -> StrategyConfig {
    StrategyConfig(
        name: "山寨币二次扫顶做空", instrumentID: "SATS-USDT-SWAP", interval: .oneHour, type: .sweepReversalShort,
        parameters: ["L": 10, "R": 5, "majorWindow": 288, "sweepWait": 96, "rejectWait": 5,
                     "resweepWait": 12, "rsiMin": 62, "volMult": 1.5, "rsDeep": 0.2,
                     "bufATR": 0.5, "tpMult": 2.2, "minATRPct": 0.5, "maxRiskATR": 5.0, "btcGateEnabled": 1],
        enabled: true, cooldownBars: 96
    )
}

/// OKX 的 K 线时间戳是 bar 的开盘时间，所以 1h 结构 bar 的收盘时刻是
/// `结构时间戳 + 60 分钟`。结构收盘后第一根可确认的 15m K 线位于 +60 分钟，
/// 窗口覆盖 +60/+75/+90/+105 四根；结构 bar 自身小时内的 +15/+30/+45 分钟
/// K 线在结构收盘前就已收盘，不能作为确认依据。
private let structureMinutes: Double = 60

private func make15mConfirmations(entryTimestamp: Date, includeConfirmation: Bool = true, delayedBeyondWindow: Bool = false) -> [Candle] {
    let first = entryTimestamp.addingTimeInterval(structureMinutes * 60)
    if delayedBeyondWindow {
        // +120 分钟正好落在半开窗口 [结构收盘, 结构收盘 + 60 分钟) 之外。
        return [Candle(timestamp: entryTimestamp.addingTimeInterval(120 * 60), open: 102.6, high: 102.7, low: 102.1, close: 102.2, volume: 20)]
    }
    if includeConfirmation {
        return [
            Candle(timestamp: first, open: 102.5, high: 102.6, low: 102.2, close: 102.3, volume: 20),
            Candle(timestamp: first.addingTimeInterval(15 * 60), open: 102.3, high: 102.5, low: 102.25, close: 102.4, volume: 20)
        ]
    }
    return [Candle(timestamp: first, open: 102.2, high: 102.4, low: 102.1, close: 102.35, volume: 20)]
}

/// 结构 bar 自身小时内的 15m 阴线（时间戳 +45 分钟，收盘于结构收盘之前）。
private func makeInsideStructureHourConfirmation(entryTimestamp: Date) -> [Candle] {
    [Candle(timestamp: entryTimestamp.addingTimeInterval(45 * 60), open: 102.5, high: 102.6, low: 102.2, close: 102.3, volume: 20)]
}

/// 窗口内第一根 +60 分钟 K 线为阳线（不确认），第一根合格的是 +75 分钟。
private func makeSecondBarConfirmation(entryTimestamp: Date) -> [Candle] {
    let first = entryTimestamp.addingTimeInterval(structureMinutes * 60)
    return [
        Candle(timestamp: first, open: 102.2, high: 102.4, low: 102.1, close: 102.35, volume: 20),
        Candle(timestamp: first.addingTimeInterval(15 * 60), open: 102.45, high: 102.5, low: 102.1, close: 102.15, volume: 20)
    ]
}

@Test
func sweepReversalKeepsIdentifierSeparateFromDisplayName() {
    let config = makeSweepConfig()
    #expect(config.strategyIdentifier == "sweepReversalShort")
    #expect(config.displayName == "山寨币二次扫顶做空")
    #expect(StrategyType.sweepReversalShort.identifier == "sweepReversalShort")
}

@Test
func sweepReversalEmitsShortSignalWithATRScaledStopAndTake() {
    let engine = StrategyEngine()
    let candles = makeSweepCandles()
    let btc = makeBTCBearishCandles()
    let status = engine.evaluate(config: makeSweepConfig(), candles: candles, btcCandles: btc)
    let signal = status.lastSignal
    #expect(signal != nil)
    #expect(signal?.type == "entry_short")
    #expect(status.direction == "short")
    // 入场价 = 二次扫顶 bar 收盘价
    let entry = NSDecimalNumber(decimal: candles[318].close).doubleValue
    #expect(abs(NSDecimalNumber(decimal: signal!.price).doubleValue - entry) < 1e-9)
    // 止损 = 扫顶期间最高价(104) + 0.5 × ATR14；止盈 = 入场 − 2.2 × 风险
    let atr = IndicatorCalculator.atrSMA(candles)[318]
    let ext = 104.0
    let expectedStop = ext + 0.5 * atr
    let expectedTake = entry - 2.2 * (expectedStop - entry)
    #expect(abs(NSDecimalNumber(decimal: signal!.stopPrice ?? 0).doubleValue - expectedStop) < 1e-9)
    #expect(abs(NSDecimalNumber(decimal: signal!.takePrice ?? 0).doubleValue - expectedTake) < 1e-9)
    // 冷却 = 96
    #expect(status.cooldown == 96)
    // 同一根 bar 重复评估不重复发信号
    let again = engine.evaluate(config: makeSweepConfig(), candles: candles, previous: status, btcCandles: btc)
    #expect(again.lastSignal?.id == signal?.id)
}

@Test
func sweepReversalBlockedWhenBTCAboveSMA200() {
    let engine = StrategyEngine()
    let candles = makeSweepCandles()
    let btc = makeBTCBullishCandles()   // BTC 在 SMA200 上方 → 门控关
    let status = engine.evaluate(config: makeSweepConfig(), candles: candles, btcCandles: btc)
    #expect(status.lastSignal == nil)
}

@Test
func sweepReversalNoSignalWithoutSecondSweep() {
    let engine = StrategyEngine()
    let candles = makeSweepCandles(resweep: false)   // 只有一次扫顶
    let btc = makeBTCBearishCandles()
    let status = engine.evaluate(config: makeSweepConfig(), candles: candles, btcCandles: btc)
    #expect(status.lastSignal == nil)
}

@Test
func sweepReversalNoSignalWithoutBTCCandles() {
    let engine = StrategyEngine()
    let candles = makeSweepCandles()
    // 未提供 BTC K 线 → 门控无法判定 → 不发信号（fail-safe）
    let status = engine.evaluate(config: makeSweepConfig(), candles: candles)
    #expect(status.lastSignal == nil)
}

@Test
func sweepReversalBlocksWhenBTCHasFewerThan200BarsBeforeSignal() {
    let engine = StrategyEngine()
    let status = engine.evaluate(config: makeSweepConfig(), candles: makeSweepCandles(), btcCandles: makeBTCWithInsufficientHistoryAtSignal())
    #expect(status.lastSignal == nil)
}

@Test
func sweepReversalUsesNearestPriorBTCBarWhenTimestampsDoNotMatchExactly() {
    let engine = StrategyEngine()
    let status = engine.evaluate(config: makeSweepConfig(), candles: makeSweepCandles(), btcCandles: makeBTCBearishCandlesWithPriorTimestamp())
    #expect(status.lastSignal != nil)
}

@Test
func sweepReversalWaitsFor15mConfirmationBeforeEmittingSignal() {
    let engine = StrategyEngine()
    let structure = makeSweepCandles()
    let btc = makeBTCBearishCandles()
    let structureTimestamp = structure[318].timestamp
    let waiting = engine.evaluateWithConfirmation(config: makeSweepConfig(), structureCandles: structure, confirmationCandles: make15mConfirmations(entryTimestamp: structureTimestamp, includeConfirmation: false), btcCandles: btc)
    #expect(waiting.lastSignal == nil)

    let confirmed = engine.evaluateWithConfirmation(config: makeSweepConfig(), structureCandles: structure, confirmationCandles: make15mConfirmations(entryTimestamp: structureTimestamp), btcCandles: btc)
    #expect(confirmed.lastSignal != nil)
    // 结构 bar 的时间戳是开盘时间，收盘后首根 15m K 线位于 +60 分钟。
    #expect(confirmed.lastSignal?.timestamp == structureTimestamp.addingTimeInterval(structureMinutes * 60))
    #expect(confirmed.lastSignal?.price == Decimal(string: "102.3"))
    #expect(confirmed.cooldown == 384)
    let atr = IndicatorCalculator.atrSMA(structure)[318]
    let expectedStop = 104.0 + 0.5 * atr
    let expectedTake = 102.3 - 2.2 * (expectedStop - 102.3)
    #expect(abs(NSDecimalNumber(decimal: confirmed.lastSignal!.stopPrice ?? 0).doubleValue - expectedStop) < 1e-9)
    #expect(abs(NSDecimalNumber(decimal: confirmed.lastSignal!.takePrice ?? 0).doubleValue - expectedTake) < 1e-9)

    let repeated = engine.evaluateWithConfirmation(config: makeSweepConfig(), structureCandles: structure, confirmationCandles: make15mConfirmations(entryTimestamp: structureTimestamp), previous: confirmed, btcCandles: btc)
    #expect(repeated.lastSignal?.id == confirmed.lastSignal?.id)
}

/// 回归：结构 bar 自身小时内的 15m 阴线在结构收盘前就已收盘，不能确认结构。
/// 修复前窗口是 `(结构时间戳, +60 分钟]`，会把 +45 分钟这根当成确认并锁死冷却。
@Test
func sweepReversalIgnoresConfirmationsFromInsideTheStructureHour() {
    let engine = StrategyEngine()
    let structure = makeSweepCandles()
    let structureTimestamp = structure[318].timestamp
    let status = engine.evaluateWithConfirmation(
        config: makeSweepConfig(),
        structureCandles: structure,
        confirmationCandles: makeInsideStructureHourConfirmation(entryTimestamp: structureTimestamp),
        btcCandles: makeBTCBearishCandles()
    )
    #expect(status.lastSignal == nil)
    #expect(status.cooldown == 0)
}

/// 回归：窗口内第一根合格 K 线出现在 +75 分钟时，入场价与信号时刻取自它。
@Test
func sweepReversalTakesTheFirstQualifyingBarInsideThePostCloseWindow() {
    let engine = StrategyEngine()
    let structure = makeSweepCandles()
    let structureTimestamp = structure[318].timestamp
    let status = engine.evaluateWithConfirmation(
        config: makeSweepConfig(),
        structureCandles: structure,
        confirmationCandles: makeSecondBarConfirmation(entryTimestamp: structureTimestamp),
        btcCandles: makeBTCBearishCandles()
    )
    #expect(status.lastSignal != nil)
    #expect(status.lastSignal?.timestamp == structureTimestamp.addingTimeInterval(75 * 60))
    #expect(status.lastSignal?.price == Decimal(string: "102.15"))
}

@Test
func sweepReversalCancelsWhen15mConfirmationWindowExpires() {
    let engine = StrategyEngine()
    let structure = makeSweepCandles()
    // 半开区间上界 +120 分钟不包含在内，因此不确认。
    let status = engine.evaluateWithConfirmation(config: makeSweepConfig(), structureCandles: structure, confirmationCandles: make15mConfirmations(entryTimestamp: structure[318].timestamp, delayedBeyondWindow: true), btcCandles: makeBTCBearishCandles())
    #expect(status.lastSignal == nil)
}

/// 回归：冷却必须按 K 线推进，不能按"被评估了多少次"推进。前端刷新图表会走 REST
/// 用同一根 K 线重复调用评估，修复前每调用一次就多扣一格冷却。
@Test
func sweepReversalCooldownAdvancesPerBarNotPerEvaluation() {
    let engine = StrategyEngine()
    let btc = makeBTCBearishCandles()
    let candles = makeSweepCandles()
    let first = engine.evaluate(config: makeSweepConfig(), candles: candles, btcCandles: btc)
    #expect(first.lastSignal != nil)
    #expect(first.cooldown == 96)

    // 同一根 K 线再评估 5 次（模拟反复刷新图表）不得改变任何状态。
    var repeated = first
    for _ in 0..<5 {
        repeated = engine.evaluate(config: makeSweepConfig(), candles: candles, previous: repeated, btcCandles: btc)
    }
    #expect(repeated.cooldown == 96)
    #expect(repeated.lastSignal?.id == first.lastSignal?.id)

    // 只有出现新的已确认 K 线才推进一格。
    var advancedCandles = candles
    advancedCandles.append(Candle(timestamp: candles[318].timestamp.addingTimeInterval(3600), open: 102.3, high: 102.4, low: 102.0, close: 102.2, volume: 100, confirmed: true))
    let advanced = engine.evaluate(config: makeSweepConfig(), candles: advancedCandles, previous: repeated, btcCandles: btc)
    #expect(advanced.cooldown == 95)
}

/// 回归：15m 执行路径的冷却同样按 bar 幂等。
@Test
func sweepReversalConfirmationCooldownAdvancesPerBarNotPerEvaluation() {
    let engine = StrategyEngine()
    let structure = makeSweepCandles()
    let btc = makeBTCBearishCandles()
    let structureTimestamp = structure[318].timestamp
    let confirmations = make15mConfirmations(entryTimestamp: structureTimestamp)
    let confirmed = engine.evaluateWithConfirmation(config: makeSweepConfig(), structureCandles: structure, confirmationCandles: confirmations, btcCandles: btc)
    #expect(confirmed.cooldown == 384)

    var repeated = confirmed
    for _ in 0..<5 {
        repeated = engine.evaluateWithConfirmation(config: makeSweepConfig(), structureCandles: structure, confirmationCandles: confirmations, previous: repeated, btcCandles: btc)
    }
    #expect(repeated.cooldown == 384)
    #expect(repeated.lastSignal?.id == confirmed.lastSignal?.id)

    var advancedConfirmations = confirmations
    advancedConfirmations.append(Candle(timestamp: structureTimestamp.addingTimeInterval(90 * 60), open: 102.2, high: 102.3, low: 102.0, close: 102.15, volume: 20))
    let advanced = engine.evaluateWithConfirmation(config: makeSweepConfig(), structureCandles: structure, confirmationCandles: advancedConfirmations, previous: repeated, btcCandles: btc)
    #expect(advanced.cooldown == 383)
}

/// 回归：1h 结构事件只能返回该标的自己的状态。修复前 1h 分支返回全局汇总状态
/// （最后被评估标的的状态），A 标的未提交的信号会在时间戳相同的 B 标的 1h 收盘时
/// 被当作 B 的信号，进而可能给 B 下错单。
@Test
func sweepReversalOneHourEventNeverLeaksAnotherInstrumentsSignal() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("novatrade-sweep-scope-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = PaperTradingStore(directory: directory)
    // 策略实例扫描动态范围；另一个标的的事件必须返回自己的中性状态，
    // 不能带出 A 标的的信号。
    let config = StrategyConfig(
        name: "山寨币二次扫顶做空", scope: .dynamic(.sweepCandidates), interval: .oneHour,
        type: .sweepReversalShort, parameters: makeSweepConfig().parameters,
        enabled: true, cooldownBars: 96
    )
    _ = try await store.create(config)

    // 两个标的都高于 sweepCandidates 的 300 万 USDT 成交额下限。
    let contracts = [
        ContractMarket(id: "SATS-USDT-SWAP", name: "SATS", baseCurrency: "SATS", quoteCurrency: "USDT", last: 1, volume24h: 10_000_000),
        ContractMarket(id: "ALT-USDT-SWAP", name: "ALT", baseCurrency: "ALT", quoteCurrency: "USDT", last: 1, volume24h: 9_000_000),
    ]
    let structure = makeSweepCandles()
    let structureTimestamp = structure[318].timestamp

    // A 标的：1h 结构收盘 → 15m 确认 → 产生信号，但本用例中从未提交订单。
    _ = await store.evaluate(MarketSnapshot(instrumentID: "BTC-USDT-SWAP", interval: .oneHour, candles: makeBTCBearishCandles()), contracts: contracts)
    _ = await store.evaluate(MarketSnapshot(instrumentID: "SATS-USDT-SWAP", interval: .oneHour, candles: structure), contracts: contracts)
    _ = await store.evaluate(MarketSnapshot(instrumentID: "SATS-USDT-SWAP", interval: .fifteenMinutes, candles: make15mConfirmations(entryTimestamp: structureTimestamp)), contracts: contracts)
    let leakedSignal = await store.status(for: config.id, instrumentID: "SATS-USDT-SWAP")?.lastSignal
    #expect(leakedSignal != nil)

    // B 标的：同一时刻的 1h 收盘事件不得带出 A 标的的信号。
    let bStatuses = await store.evaluate(MarketSnapshot(instrumentID: "ALT-USDT-SWAP", interval: .oneHour, candles: structure), contracts: contracts)
    #expect(bStatuses.count == 1)
    #expect(await store.status(for: config.id, instrumentID: "ALT-USDT-SWAP")?.lastSignal == nil)
    // A 标的自己的信号必须仍然可见：修复不能靠"清空状态"实现。
    #expect(await store.status(for: config.id, instrumentID: "SATS-USDT-SWAP")?.lastSignal?.id == leakedSignal?.id)
}
