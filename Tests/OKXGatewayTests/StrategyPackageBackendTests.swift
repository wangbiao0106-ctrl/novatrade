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
      "identifier": "double_pump_exhaustion_short", "version": "1.0.0", "display_name": "DME",
      "runtime": { "runtime_handler": "doublePumpExhaustionShort", "scope": "dynamic.doublePumpCandidates", "enabled_by_default": false },
      "entry_timeframe_minutes": 15,
      "signal_parameters": { "swing_lookback": 6 }
    }
    """.data(using: .utf8)!
    try config.write(to: staging.appendingPathComponent("config/strategy.json"))
    defer { try? fm.removeItem(at: root) }

    let paper = PaperTradingStore(directory: state)
    let registry = StrategyPackageRegistry(directory: packages)
    let risk = RiskEngine(initialEquity: 100_000)
    await risk.synchronizeStrategyCapital(100_000)
    let backend = TradingBackend(paper: paper, riskEngine: risk, strategyPackages: registry)
    let installed = try await backend.installStrategyPackage(StrategyPackageInstallRequest(path: "demo"))
    #expect(installed.identifier == "double_pump_exhaustion_short")
    #expect((await backend.strategyPackageManifests()).count == 1)
    let created = try await backend.createStrategyFromPackage(identifier: "double_pump_exhaustion_short")
    #expect(created.type == .doublePumpExhaustionShort)
    #expect(created.enabled == false)
    _ = try await backend.uninstallStrategyPackage(identifier: "double_pump_exhaustion_short")
    #expect((await backend.strategyPackageManifests()).isEmpty)
    #expect((await backend.strategies()).isEmpty)
}
