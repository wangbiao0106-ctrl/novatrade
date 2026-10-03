import Foundation
import CryptoKit
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
    let rule = Data("# Sweep\n".utf8)
    let config = """
    {
      "identifier": "sweep_reversal_short", "version": "1.0.0", "display_name": "Sweep",
      "runtime": { "runtime_handler": "sweepReversalShort", "scope": "dynamic.sweepCandidates", "enabled_by_default": false },
      "entry_timeframe_minutes": 60,
      "signal_parameters": { "L": 10 }
    }
    """.data(using: .utf8)!
    try rule.write(to: staging.appendingPathComponent("STRATEGY.md"))
    try config.write(to: staging.appendingPathComponent("config/strategy.json"))
    let ruleDigest = SHA256.hash(data: rule).map { String(format: "%02x", $0) }.joined()
    let configDigest = SHA256.hash(data: config).map { String(format: "%02x", $0) }.joined()
    let manifest = """
    { "schema_version": 1, "strategy_id": "sweep_reversal_short", "package_id": "sweep_reversal_short", "version": "1.0.0", "display_name": "Sweep", "runtime_handler": "sweepReversalShort", "lifecycle": "finalized", "artifacts": [{"path":"STRATEGY.md","sha256":"\(ruleDigest)"},{"path":"config/strategy.json","sha256":"\(configDigest)"}] }
    """
    try Data(manifest.utf8).write(to: staging.appendingPathComponent("manifest.json"))
    defer { try? fm.removeItem(at: root) }

    let paper = PaperTradingStore(directory: state)
    let registry = StrategyPackageRegistry(directory: packages)
    let risk = RiskEngine(initialEquity: 100_000)
    await risk.synchronizeStrategyCapital(100_000)
    let backend = TradingBackend(paper: paper, riskEngine: risk, strategyPackages: registry)
    let installed = try await backend.installStrategyPackage(StrategyPackageInstallRequest(path: "demo"))
    #expect(installed.identifier == "sweep_reversal_short")
    #expect((await backend.strategyPackageManifests()).count == 1)
    let created = try await backend.createStrategyFromPackage(identifier: "sweep_reversal_short")
    #expect(created.type == .sweepReversalShort)
    #expect(created.enabled == false)
    _ = try await backend.uninstallStrategyPackage(identifier: "sweep_reversal_short")
    #expect((await backend.strategyPackageManifests()).isEmpty)
    #expect((await backend.strategies()).isEmpty)
}
