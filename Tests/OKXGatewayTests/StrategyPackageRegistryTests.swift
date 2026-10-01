import Foundation
import CryptoKit
import Testing
@testable import TradingDomain

@Test("Strategy package manifests adapt the existing experiment config shape")
func experimentManifestIsParsed() throws {
    let json = """
    {
      "strategy": "HLSR",
      "version": "1.0",
      "name_zh": "高位扫顶反转",
      "display_name": "高位扫顶反转做空",
      "name_en": "High-Level Liquidity Sweep Reversal",
      "runtime": {
        "strategy_type": "hlsr",
        "scope": "dynamic.hotAltcoins",
        "auto_submit_live_orders": false
      },
      "entry_timeframe_minutes": 15,
      "signal_parameters": { "swing_lookback": 6, "allow_range": true },
      "position_management": { "leverage": 2.0, "risk_per_trade_pct": 0.5, "risk_per_trade_max_pct": 5.0, "cooldown_bars": 16 }
    }
    """.data(using: .utf8)!
    let manifest = try StrategyPackageRegistry.decodeManifest(json)
    #expect(manifest.identifier == "hlsr")
    #expect(manifest.runtimeHandler == "hlsr")
    #expect(manifest.displayName == "高位扫顶反转做空")
    #expect(manifest.entryTimeframeMinutes == 15)
    #expect(manifest.defaultParameters["swingLookback"] == 6)
    #expect(manifest.defaultParameters["allowRange"] == 1)
    #expect(manifest.defaultParameters["leverage"] == 2)
    #expect(manifest.executableStrategyType == .hlsr)
    let config = try manifest.makeDefaultConfiguration()
    #expect(config.type == .hlsr)
    #expect(config.interval == .fifteenMinutes)
    #expect(config.parameters["swingLookback"] == 6)
    #expect(config.parameters["leverage"] == 2)
    #expect(config.enabled == false)
}

@Test("Installing and uninstalling a package is isolated to the registry directory")
func packageInstallAndUninstall() async throws {
    let fm = FileManager.default
    let root = fm.temporaryDirectory.appendingPathComponent("strategy-registry-\(UUID().uuidString)", isDirectory: true)
    let source = root.appendingPathComponent("hlsr", isDirectory: true)
    let installed = root.appendingPathComponent("installed", isDirectory: true)
    try fm.createDirectory(at: source.appendingPathComponent("config"), withIntermediateDirectories: true)
    let json = """
    { "identifier": "demo", "version": "1.0.0", "displayName": "Demo", "runtimeHandler": "demo", "parameters": { "x": 1 } }
    """.data(using: .utf8)!
    try json.write(to: source.appendingPathComponent("config/strategy.json"))
    defer { try? fm.removeItem(at: root) }

    let registry = StrategyPackageRegistry(directory: installed)
    let package = try await registry.install(package: source)
    #expect(package.manifest.identifier == "demo")
    #expect((await registry.installed()).count == 1)
    #expect(fm.fileExists(atPath: installed.appendingPathComponent("demo/config/strategy.json").path))
    _ = try await registry.uninstall(identifier: "demo")
    #expect((await registry.installed()).isEmpty)
}

@Test("Unknown handlers can be staged but are not treated as executable")
func unknownHandlerIsDormant() throws {
    let json = """
    { "identifier": "future", "version": "1", "displayName": "Future", "runtimeHandler": "future-v2" }
    """.data(using: .utf8)!
    let manifest = try StrategyPackageRegistry.decodeManifest(json)
    #expect(manifest.executableStrategyType == nil)
    #expect(manifest.autoSubmitLiveOrders == false)
    #expect(manifest.enabledByDefault == false)
}

@Test("Legacy strategy names map to stable runtime adapter identifiers")
func legacyHandlerAliases() throws {
    let json = """
    { "strategy": "SWEEP_REVERSAL_SHORT", "version": "1.3", "name_zh": "扫顶做空" }
    """.data(using: .utf8)!
    let manifest = try StrategyPackageRegistry.decodeManifest(json)
    #expect(manifest.identifier == "sweep_reversal_short")
    #expect(manifest.runtimeHandler == "sweepReversalShort")
    #expect(manifest.executableStrategyType == .sweepReversalShort)
}

@Test("Root package manifest keeps finalized lifecycle and tuned config values")
func packageManifestFields() async throws {
    let fm = FileManager.default
    let root = fm.temporaryDirectory.appendingPathComponent("strategy-manifest-\(UUID().uuidString)", isDirectory: true)
    let package = root.appendingPathComponent("hlsr", isDirectory: true)
    try fm.createDirectory(at: package.appendingPathComponent("config"), withIntermediateDirectories: true)
    let manifest = """
    { "schema_version": 1, "strategy_id": "hlsr", "package_id": "hlsr", "version": "2.0.0", "display_name": "HLSR 2", "runtime_handler": "hlsr", "lifecycle": "finalized" }
    """.data(using: .utf8)!
    let config = """
    { "strategy": "HLSR", "version": "1.0.0", "display_name": "HLSR", "runtime": { "strategy_type": "hlsr" }, "signal_parameters": { "swing_lookback": 9 }, "position_management": { "leverage": 2.0 } }
    """.data(using: .utf8)!
    try fm.createDirectory(at: package, withIntermediateDirectories: true)
    try manifest.write(to: package.appendingPathComponent("manifest.json"))
    try config.write(to: package.appendingPathComponent("config/strategy.json"))
    let registry = StrategyPackageRegistry(directory: root.appendingPathComponent("installed"))
    defer { try? fm.removeItem(at: root) }
    let installed = try await registry.install(package: package)
    #expect(installed.manifest.lifecycle == "finalized")
    #expect(installed.manifest.version == "2.0.0")
    #expect(installed.manifest.defaultParameters["swingLookback"] == 9)
    #expect(installed.manifest.defaultParameters["leverage"] == 2)
}

@Test("Swift registry rejects a tampered artifact before replacement")
func swiftRegistryVerifiesArtifactHashes() async throws {
    let fm = FileManager.default
    let root = fm.temporaryDirectory.appendingPathComponent("strategy-artifact-\(UUID().uuidString)", isDirectory: true)
    let package = root.appendingPathComponent("demo", isDirectory: true)
    try fm.createDirectory(at: package.appendingPathComponent("config"), withIntermediateDirectories: true)
    let rule = Data("# Demo\n".utf8)
    try rule.write(to: package.appendingPathComponent("STRATEGY.md"))
    let config = Data("{\"identifier\":\"demo\",\"version\":\"1.0.0\",\"display_name\":\"Demo\",\"runtime\":{\"strategy_type\":\"demo\"}}".utf8)
    try config.write(to: package.appendingPathComponent("config/strategy.json"))
    let digest = SHA256.hash(data: rule).map { String(format: "%02x", $0) }.joined()
    let manifest = """
    { "schema_version": 1, "strategy_id": "demo", "package_id": "demo", "version": "1.0.0", "display_name": "Demo", "runtime_handler": "demo", "lifecycle": "finalized", "artifacts": [{"path":"STRATEGY.md","sha256":"\(digest)"}] }
    """
    try Data(manifest.utf8).write(to: package.appendingPathComponent("manifest.json"))
    try Data("tampered".utf8).write(to: package.appendingPathComponent("STRATEGY.md"))
    defer { try? fm.removeItem(at: root) }
    let registry = StrategyPackageRegistry(directory: root.appendingPathComponent("installed"))
    do {
        _ = try await registry.install(package: package)
        Issue.record("tampered package unexpectedly installed")
    } catch let error as StrategyPackageError {
        guard case .invalidPackage = error else { Issue.record("unexpected error: \(error)"); return }
    }
}
