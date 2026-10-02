import Foundation
import Testing
import TradingDomain

@Test("AccountOverview exposes only authenticated USDT equity")
func accountOverviewUSDTEquityIsScopedToUSDTAssets() {
    let assets = [
        AccountAsset(currency: "btc", equity: 2_000, available: 2_000, usdValue: 2_000),
        AccountAsset(currency: "uSdT", equity: 125.5, available: 100),
        AccountAsset(currency: "USDT", equity: 24.5, available: 24),
        AccountAsset(currency: "ETH", equity: 9_000, available: 9_000)
    ]

    let unauthenticated = AccountOverview(authenticated: false, assets: assets)
    #expect(unauthenticated.usdtEquity == nil)

    let authenticated = AccountOverview(authenticated: true, assets: assets)
    #expect(authenticated.usdtEquity == 150)
}

@Test("USDT equity ignores other assets, handles absence, nonfinite values, and negative net")
func accountOverviewUSDTEquityValidation() {
    let noUSDT = AccountOverview(authenticated: true, assets: [
        AccountAsset(currency: "BTC", equity: 10_000),
        AccountAsset(currency: "USDC", equity: 2_000)
    ])
    #expect(noUSDT.usdtEquity == 0)

    let negativeNet = AccountOverview(authenticated: true, assets: [
        AccountAsset(currency: "usdt", equity: -12),
        AccountAsset(currency: "USDT", equity: 4),
        AccountAsset(currency: "BTC", equity: Decimal.nan)
    ])
    // A negative USDT net balance is safe to report as zero. A nonfinite
    // non-USDT asset must not poison the USDT-only calculation.
    #expect(negativeNet.usdtEquity == 0)

    let invalidUSDT = AccountOverview(authenticated: true, assets: [
        AccountAsset(currency: "USDT", equity: Decimal.nan),
        AccountAsset(currency: "BTC", equity: 10_000)
    ])
    #expect(invalidUSDT.usdtEquity == nil)
}

@Test("Strategy capital allocation counts assigned equity even with no reservations")
func strategyCapitalAllocationUsesPoolEquity() {
    let pausedPool = StrategyCapitalSnapshot(
        strategyID: UUID(), allocationPercent: 20, initialCapital: 200,
        equity: 220, reservedCapital: 0
    )
    let compoundedPool = StrategyCapitalSnapshot(
        strategyID: UUID(), allocationPercent: 30, initialCapital: 300,
        equity: 360, reservedCapital: 40
    )
    let allocation = StrategyCapitalAllocation(
        totalCapital: 1_000,
        strategyCapitals: [pausedPool, compoundedPool]
    )

    #expect(allocation.totalCapital == 1_000)
    #expect(allocation.occupiedCapital == 580)
    #expect(allocation.availableCapital == 420)
    #expect(allocation.occupiedAllocationPercent == 58)
    #expect(allocation.availableAllocationPercent == 42)
    #expect(allocation.capital(for: 50) == 500)
    #expect(allocation.capital(for: 0) == 0)
    #expect(allocation.capital(for: 200) == 1_000)
    #expect(allocation.effectiveAllocationPercent(for: 20) == 20)
    #expect(allocation.effectiveAllocationPercent(for: 50) == 42)
    #expect(allocation.effectiveAllocationPercent(for: 100) == 42)
}

@Test("Strategy capital allocation handles zero total capital")
func strategyCapitalAllocationWithZeroBalance() {
    let allocation = StrategyCapitalAllocation(
        totalCapital: 0,
        strategyCapitals: [
            StrategyCapitalSnapshot(strategyID: UUID(), allocationPercent: 75, equity: 100)
        ]
    )

    #expect(allocation.totalCapital == 0)
    #expect(allocation.occupiedCapital == 100)
    #expect(allocation.occupiedAllocationPercent == 0)
    #expect(allocation.availableCapital == 0)
    #expect(allocation.availableAllocationPercent == 0)
    #expect(allocation.capital(for: 50) == 0)
    #expect(allocation.effectiveAllocationPercent(for: 100) == 0)
}

@Test("Strategy capital allocation clamps negative pool equity")
func strategyCapitalAllocationClampsNegativeEquity() {
    let allocation = StrategyCapitalAllocation(
        totalCapital: 1_000,
        strategyCapitals: [
            StrategyCapitalSnapshot(strategyID: UUID(), allocationPercent: 20, equity: -100),
            StrategyCapitalSnapshot(strategyID: UUID(), allocationPercent: 10, equity: 100)
        ]
    )

    #expect(allocation.occupiedCapital == 100)
    #expect(allocation.occupiedAllocationPercent == 10)
    #expect(allocation.availableAllocationPercent == 70)
    #expect(allocation.capital(for: 90) == 900)
    #expect(allocation.effectiveAllocationPercent(for: 90) == 70)
}
