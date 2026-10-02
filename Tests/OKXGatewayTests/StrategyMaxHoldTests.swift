import Testing
import TradingDomain
@testable import TradingService

@Test
func strategyMaxHoldMatchesEachSpec() {
    // Double pump: 24 x 15m bars counted from the bar after the signal bar.
    let doublePump = StrategyConfig(name: "双泵", scope: .dynamic(.hotAltcoins), interval: .fifteenMinutes, type: .doublePumpExhaustionShort)
    #expect(TradingBackend.strategyMaxHoldSeconds(doublePump) == 25 * 15 * 60)
    let sweep = StrategyConfig(name: "扫荡", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort)
    #expect(TradingBackend.strategyMaxHoldSeconds(sweep) == 96 * 3600)
}
