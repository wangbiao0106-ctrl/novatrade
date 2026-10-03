import Foundation
import CryptoKit
import Testing
@testable import TradingDomain

@Test("Strategy package manifests adapt the existing experiment config shape")
func experimentManifestIsParsed() throws {
    let json = """
    {
      "strategy": "SWEEP_REVERSAL_SHORT",
      "version": "1.0",
      "name_zh": "山寨币二次扫顶",
      "display_name": "山寨币二次扫顶",
      "name_en": "Sweep Reversal Short",
      "runtime": {
        "strategy_type": "sweepReversalShort",
        "scope": "dynamic.sweepCandidates",
        "auto_submit_live_orders": false
      },
      "entry_timeframe_minutes": 60,
      "signal_parameters": { "atr_period": 14, "rsi_period": 14 },
      "position_management": { "leverage": 2.0, "risk_per_trade_pct": 1.0, "risk_per_trade_max_pct": 1.0, "cooldown_bars": 96 }
    }
    """.data(using: .utf8)!
    let manifest = try StrategyPackageRegistry.decodeManifest(json)
    #expect(manifest.identifier == "sweep_reversal_short")
    #expect(manifest.runtimeHandler == "sweepReversalShort")
    #expect(manifest.displayName == "山寨币二次扫顶")
    #expect(manifest.entryTimeframeMinutes == 60)
    #expect(manifest.defaultParameters["atrPeriod"] == 14)
    #expect(manifest.defaultParameters["rsiPeriod"] == 14)
    #expect(manifest.defaultParameters["leverage"] == 2)
    #expect(manifest.executableStrategyType == .sweepReversalShort)
    let config = try manifest.makeDefaultConfiguration()
    #expect(config.type == .sweepReversalShort)
    #expect(config.interval == .oneHour)
    #expect(config.parameters["atrPeriod"] == 14)
    #expect(config.parameters["leverage"] == 2)
    #expect(config.enabled == false)
}

@Test("Installing and uninstalling a package is isolated to the registry directory")
func packageInstallAndUninstall() async throws {
    let fm = FileManager.default
    let root = fm.temporaryDirectory.appendingPathComponent("strategy-registry-\(UUID().uuidString)", isDirectory: true)
    let source = root.appendingPathComponent("demo", isDirectory: true)
    let installed = root.appendingPathComponent("installed", isDirectory: true)
    try fm.createDirectory(at: source.appendingPathComponent("config"), withIntermediateDirectories: true)
    let rule = Data("# Demo\n".utf8)
    let json = """
    { "identifier": "demo", "version": "1.0.0", "display_name": "Demo", "runtime_handler": "demo", "parameters": { "x": 1 } }
    """.data(using: .utf8)!
    try rule.write(to: source.appendingPathComponent("STRATEGY.md"))
    try json.write(to: source.appendingPathComponent("config/strategy.json"))
    let ruleDigest = SHA256.hash(data: rule).map { String(format: "%02x", $0) }.joined()
    let configDigest = SHA256.hash(data: json).map { String(format: "%02x", $0) }.joined()
    let manifest = """
    { "schema_version": 1, "strategy_id": "demo", "package_id": "demo", "version": "1.0.0", "display_name": "Demo", "runtime_handler": "demo", "lifecycle": "finalized", "artifacts": [{"path":"STRATEGY.md","sha256":"\(ruleDigest)"},{"path":"config/strategy.json","sha256":"\(configDigest)"}] }
    """
    try Data(manifest.utf8).write(to: source.appendingPathComponent("manifest.json"))
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
    { "identifier": "future", "version": "1", "display_name": "Future", "runtime_handler": "future-v2" }
    """.data(using: .utf8)!
    let manifest = try StrategyPackageRegistry.decodeManifest(json)
    #expect(manifest.executableStrategyType == nil)
    #expect(manifest.autoSubmitLiveOrders == false)
    #expect(manifest.enabledByDefault == false)
}

@Test("Research strategy names map to stable runtime adapter identifiers")
func researchHandlerAliases() throws {
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
    let package = root.appendingPathComponent("sweep_reversal_short", isDirectory: true)
    try fm.createDirectory(at: package.appendingPathComponent("config"), withIntermediateDirectories: true)
    let rule = Data("# Sweep\n".utf8)
    let manifest = """
    { "schema_version": 1, "strategy_id": "sweep_reversal_short", "package_id": "sweep_reversal_short", "version": "2.0.0", "display_name": "Sweep 2", "runtime_handler": "sweepReversalShort", "lifecycle": "finalized" }
    """.data(using: .utf8)!
    let config = """
    { "strategy": "SWEEP_REVERSAL_SHORT", "version": "1.0.0", "display_name": "Sweep", "runtime": { "strategy_type": "sweepReversalShort" }, "signal_parameters": { "atr_period": 9 }, "position_management": { "leverage": 2.0 } }
    """.data(using: .utf8)!
    try fm.createDirectory(at: package, withIntermediateDirectories: true)
    try rule.write(to: package.appendingPathComponent("STRATEGY.md"))
    try manifest.write(to: package.appendingPathComponent("manifest.json"))
    try config.write(to: package.appendingPathComponent("config/strategy.json"))
    let ruleDigest = SHA256.hash(data: rule).map { String(format: "%02x", $0) }.joined()
    let configDigest = SHA256.hash(data: config).map { String(format: "%02x", $0) }.joined()
    let manifestWithArtifacts = """
    { "schema_version": 1, "strategy_id": "sweep_reversal_short", "package_id": "sweep_reversal_short", "version": "2.0.0", "display_name": "Sweep 2", "runtime_handler": "sweepReversalShort", "lifecycle": "finalized", "artifacts": [{"path":"STRATEGY.md","sha256":"\(ruleDigest)"},{"path":"config/strategy.json","sha256":"\(configDigest)"}] }
    """
    try Data(manifestWithArtifacts.utf8).write(to: package.appendingPathComponent("manifest.json"))
    let registry = StrategyPackageRegistry(directory: root.appendingPathComponent("installed"))
    defer { try? fm.removeItem(at: root) }
    let installed = try await registry.install(package: package)
    #expect(installed.manifest.lifecycle == "finalized")
    #expect(installed.manifest.version == "2.0.0")
    #expect(installed.manifest.defaultParameters["atrPeriod"] == 9)
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
    let configDigest = SHA256.hash(data: config).map { String(format: "%02x", $0) }.joined()
    let manifest = """
    { "schema_version": 1, "strategy_id": "demo", "package_id": "demo", "version": "1.0.0", "display_name": "Demo", "runtime_handler": "demo", "lifecycle": "finalized", "artifacts": [{"path":"STRATEGY.md","sha256":"\(digest)"},{"path":"config/strategy.json","sha256":"\(configDigest)"}] }
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

@Test("Runtime package installation requires a finalized root manifest")
func configOnlyPackageIsRejected() async throws {
    let fm = FileManager.default
    let root = fm.temporaryDirectory.appendingPathComponent("strategy-config-only-\(UUID().uuidString)", isDirectory: true)
    let package = root.appendingPathComponent("demo", isDirectory: true)
    try fm.createDirectory(at: package.appendingPathComponent("config"), withIntermediateDirectories: true)
    try Data("{}".utf8).write(to: package.appendingPathComponent("config/strategy.json"))
    defer { try? fm.removeItem(at: root) }
    let registry = StrategyPackageRegistry(directory: root.appendingPathComponent("installed"))
    do {
        _ = try await registry.install(package: package)
        Issue.record("config-only package unexpectedly installed")
    } catch let error as StrategyPackageError {
        guard case .invalidPackage = error else { Issue.record("unexpected error: \(error)"); return }
    }
}

@Test("Manifest integer parsing rejects values outside Int range")
func manifestRejectsOversizedIntegers() throws {
    let json = """
    { "identifier": "demo", "version": "1", "display_name": "Demo", "entry_timeframe_minutes": 1e308 }
    """.data(using: .utf8)!
    do {
        _ = try StrategyPackageRegistry.decodeManifest(json)
        Issue.record("oversized integer unexpectedly decoded")
    } catch let error as StrategyPackageError {
        guard case .invalidManifest = error else { Issue.record("unexpected error: \(error)"); return }
    }
}

@Test("Manifest integer parsing rejects fractional values")
func manifestRejectsFractionalIntegers() throws {
    let json = """
    { "identifier": "demo", "version": "1", "display_name": "Demo", "entry_timeframe_minutes": 1.2 }
    """.data(using: .utf8)!
    do {
        _ = try StrategyPackageRegistry.decodeManifest(json)
        Issue.record("fractional integer unexpectedly decoded")
    } catch let error as StrategyPackageError {
        guard case .invalidManifest = error else { Issue.record("unexpected error: \(error)"); return }
    }
}
