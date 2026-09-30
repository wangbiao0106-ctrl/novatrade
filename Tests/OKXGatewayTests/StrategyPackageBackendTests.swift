import Foundation
import Testing
import TradingDomain
@testable import TradingService

@Test("Backend installs a staged package and creates its disabled instance")
func backendStrategyPackageLifecycle() async throws {
    let fm = FileManager.default
    let root = fm.temporaryDirectory.appendingPathComponent("strategy-backend-\(UUID().uuidString)", isDirectory: true)
    let state = root.appendingPathComponent("state", isDirectory: true)
    let staging = state.appendingPathComponent("strategy-staging/demo", isDirectory: true)
    let packages = root.appendingPathComponent("packages", isDirectory: true)
    try fm.createDirectory(at: staging.appendingPathComponent("config"), withIntermediateDirectories: true)
    let config = """
    {
      "identifier": "hlsr", "version": "1.0.0", "display_name": "HLSR",
      "runtime": { "strategy_type": "hlsr", "scope": "dynamic.hotAltcoins", "enabled_by_default": false },
      "entry_timeframe_minutes": 15,
      "signal_parameters": { "swing_lookback": 6 }
    }
    """.data(using: .utf8)!
    try config.write(to: staging.appendingPathComponent("config/strategy.json"))
    defer { try? fm.removeItem(at: root) }

    let paper = PaperTradingStore(directory: state)
    let registry = StrategyPackageRegistry(directory: packages)
    let backend = TradingBackend(paper: paper, strategyPackages: registry)
    let installed = try await backend.installStrategyPackage(StrategyPackageInstallRequest(path: "demo"))
    #expect(installed.identifier == "hlsr")
    #expect((await backend.strategyPackageManifests()).count == 1)
    let created = try await backend.createStrategyFromPackage(identifier: "hlsr")
    #expect(created.type == .hlsr)
    #expect(created.enabled == false)
    _ = try await backend.uninstallStrategyPackage(identifier: "hlsr")
    #expect((await backend.strategyPackageManifests()).isEmpty)
    #expect((await backend.strategies()).isEmpty)
}
