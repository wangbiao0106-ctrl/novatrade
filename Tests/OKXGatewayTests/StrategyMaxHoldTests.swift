import Testing
import TradingDomain
@testable import TradingService

@Test
func strategyMaxHoldMatchesEachSpec() {
    let sweep = StrategyConfig(name: "扫荡", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort)
    #expect(TradingBackend.strategyMaxHoldSeconds(sweep) == 96 * 3600)
}
