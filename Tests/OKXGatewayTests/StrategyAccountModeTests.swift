import Foundation
import Testing
import TradingDomain
@testable import TradingService

@Test("Every executable strategy can be configured without an account mode")
func runtimeStrategiesDoNotEncodeAnAccountExecutionMode() async throws {
    for type in StrategyType.availableCases where type.hasRuntimeHandler {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("strategy-account-mode-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = PaperTradingStore(directory: directory)
        let input = StrategyConfig(name: type.displayName,
                                   scope: type.defaultScope,
                                   interval: type.entryInterval,
                                   type: type)
        let created = try await store.create(input)

        #expect(created.type == type)
        #expect(created.interval == type.entryInterval)
        #expect(created.scope == type.defaultScope)
    }
}

@Test("Legacy package execution metadata does not alter the runtime strategy")
func packageExecutionMetadataCannotRestrictStrategyMode() throws {
    let demoOnly = try StrategyPackageManifest(
        identifier: "demo-only",
        version: "1.0.0",
        displayName: "Demo-only metadata",
        runtimeHandler: "sweepReversalShort",
        liveOrderMode: "okx_demo_only",
        autoSubmitLiveOrders: false
    )
    let liveMetadata = try StrategyPackageManifest(
        identifier: "live-metadata",
        version: "1.0.0",
        displayName: "Live metadata",
        runtimeHandler: "sweepReversalShort",
        liveOrderMode: "okx_live",
        autoSubmitLiveOrders: true
    )

    let demoConfig = try demoOnly.makeDefaultConfiguration()
    let liveConfig = try liveMetadata.makeDefaultConfiguration()
    #expect(demoConfig.type == .sweepReversalShort)
    #expect(liveConfig.type == demoConfig.type)
    #expect(liveConfig.interval == demoConfig.interval)
    #expect(liveConfig.parameters == demoConfig.parameters)
}
