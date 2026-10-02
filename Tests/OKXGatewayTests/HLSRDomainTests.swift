import Foundation
import Testing
@testable import TradingDomain

@Test("HLSR is exposed with stable identity and user-facing names")
func hlsrIdentityAndDisplayName() {
    #expect(StrategyType.hlsr.identifier == "hlsr")
    #expect(StrategyType.hlsr.displayName == "高位扫顶反转做空")
    #expect(StrategyType.hlsr.englishName == "High-Level Liquidity Sweep Reversal")
    #expect(StrategyType.availableCases.contains(.hlsr))
}

@Test("HLSR domain defaults mirror its 15m and 4H execution contract")
func hlsrDefaults() {
    let type = StrategyType.hlsr
    #expect(type.entryInterval == .fifteenMinutes)
    #expect(type.defaultCooldownBars == 16)
    // Risk is a share of the strategy's own pool equity, not of the account.
    #expect(type.defaultRiskPercent == 10.0)
    #expect(type.maxRiskPercent == 10.0)
    #expect(type.maxOpenRiskPercent == 10.0)
    #expect(type.defaultParameters["maxOpenRiskPercent"] == 10.0)
    #expect(type.defaultParameters["structureTimeframeMinutes"] == 240)
    #expect(type.defaultParameters["gain24hGt"] == 0.4)
    #expect(type.defaultParameters["quoteVolume24hGt"] == 30_000_000)
    #expect(type.defaultParameters["partialTarget1"] == 0.3)
    #expect(type.defaultParameters["partialTarget2"] == 0.3)
    #expect(type.defaultParameters["partialTarget3"] == 0.4)
    #expect(type.defaultParameters["leverage"] == 2)
}

@Test("All built-in strategies expose a two-times default leverage")
func builtInStrategyDefaultLeverage() {
    for type in StrategyType.availableCases {
        #expect(type.defaultParameters["leverage"] == 2)
    }
}

@Test("HLSR strategy config persists its 15m entry interval")
func hlsrConfigInterval() throws {
    let config = StrategyConfig(
        name: StrategyType.hlsr.displayName,
        scope: .dynamic(.hotAltcoins),
        interval: StrategyType.hlsr.entryInterval,
        type: .hlsr,
        parameters: StrategyType.hlsr.defaultParameters,
        cooldownBars: StrategyType.hlsr.defaultCooldownBars
    )
    let data = try JSONEncoder().encode(config)
    let decoded = try JSONDecoder().decode(StrategyConfig.self, from: data)
    #expect(decoded.type == .hlsr)
    #expect(decoded.interval == .fifteenMinutes)
    #expect(decoded.cooldownBars == 16)
}
