import Foundation
import CryptoKit

/// A machine readable strategy package manifest.
///
/// Strategy research lives in a package (normally a directory containing
/// `config/strategy.json`).  The trading process copies an approved package to
/// its application data directory and only reads that installed copy.  This
/// keeps runtime independent from the repository's `strategies/` directory
/// while making installation and removal data driven.
public struct StrategyPackageManifest: Codable, Equatable, Sendable, Identifiable {
    public let identifier: String
    public var id: String { identifier }
    public let packageID: String?
    public let schemaVersion: Int?
    public let version: String
    public let displayName: String
    public let englishName: String
    /// The executable adapter name.  Existing adapters use values such as
    /// `sweepReversalShort`. A package may be installed before its adapter is
    /// shipped; it will then remain dormant.
    public let runtimeHandler: String
    public let sourceOfTruth: String?
    public let entryTimeframeMinutes: Int?
    public let defaultParameters: [String: Double]
    public let defaultCooldownBars: Int?
    public let scope: String?
    /// Legacy package metadata retained for decoding and catalog display. The
    /// runtime ignores both fields and routes strategy orders from the
    /// authenticated account mode.
    public let liveOrderMode: String?
    public let autoSubmitLiveOrders: Bool
    public let enabledByDefault: Bool
    /// `candidate`/`draft` packages can be staged in the laboratory but are
    /// refused by the runtime installer. Research directories without a root
    /// manifest have no lifecycle and remain readable for development imports.
    public let lifecycle: String?

    public init(identifier: String,
                version: String,
                displayName: String,
                englishName: String = "",
                runtimeHandler: String? = nil,
                packageID: String? = nil,
                schemaVersion: Int? = nil,
                sourceOfTruth: String? = nil,
                entryTimeframeMinutes: Int? = nil,
                defaultParameters: [String: Double] = [:],
                defaultCooldownBars: Int? = nil,
                scope: String? = nil,
                liveOrderMode: String? = nil,
                autoSubmitLiveOrders: Bool = false,
                enabledByDefault: Bool = false,
                lifecycle: String? = nil) throws {
        let normalizedIdentifier = Self.normalizePackageIdentifier(identifier)
        guard !normalizedIdentifier.isEmpty else {
            throw StrategyPackageError.invalidManifest("identifier 不能为空")
        }
        guard !version.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw StrategyPackageError.invalidManifest("version 不能为空")
        }
        guard !displayName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw StrategyPackageError.invalidManifest("displayName 不能为空")
        }
        self.identifier = normalizedIdentifier
        self.packageID = packageID.map(Self.normalizePackageIdentifier)
        self.schemaVersion = schemaVersion
        self.version = version
        self.displayName = displayName
        self.englishName = englishName.isEmpty ? displayName : englishName
        self.runtimeHandler = Self.canonicalIdentifier(runtimeHandler ?? identifier)
        self.sourceOfTruth = sourceOfTruth
        self.entryTimeframeMinutes = entryTimeframeMinutes
        self.defaultParameters = defaultParameters.filter { $0.key.isEmpty == false && $0.value.isFinite }
        self.defaultCooldownBars = defaultCooldownBars
        self.scope = scope
        self.liveOrderMode = liveOrderMode
        // Retain this legacy metadata for backwards-compatible decoding. The
        // runtime routes strategy orders from the connected account mode and
        // never lets a package select or forbid paper/live execution.
        self.autoSubmitLiveOrders = autoSubmitLiveOrders
        self.enabledByDefault = enabledByDefault
        self.lifecycle = lifecycle
    }

    /// Whether the package has a compiled adapter understood by the current
    /// binary.  This deliberately does not make installation depend on the
    /// adapter so a package can be staged before an app update.
    public var executableStrategyType: StrategyType? {
        [runtimeHandler, identifier]
            .compactMap { StrategyType(rawValue: Self.canonicalIdentifier($0)) }
            .first { $0.hasRuntimeHandler }
    }

    /// Builds the user-facing strategy instance defaults from an installed
    /// package.  This is the bridge used by the strategy laboratory: package
    /// metadata supplies the tunable values while the compiled adapter
    /// supplies the actual signal/evaluation implementation.
    public func makeDefaultConfiguration(id: UUID = UUID(), name: String? = nil,
                                         scope requestedScope: StrategyScope? = nil) throws -> StrategyConfig {
        guard let type = executableStrategyType else {
            throw StrategyPackageError.invalidPackage("策略包尚未安装可用的运行时适配器：\(runtimeHandler)")
        }
        var parameters = type.defaultParameters
        for (key, value) in defaultParameters { parameters[key] = value }
        let interval = type.entryInterval
        let cooldown = defaultCooldownBars ?? type.defaultCooldownBars
        return StrategyConfig(id: id,
                              name: name ?? displayName,
                              scope: requestedScope ?? type.defaultScope,
                              interval: interval,
                              type: type,
                              parameters: parameters,
                              enabled: false,
                              cooldownBars: cooldown)
    }

    /// Normalizes the on-disk/package identifier. Package IDs intentionally
    /// retain snake_case so Swift and the laboratory CLI address the same
    /// directory. Runtime handler aliases are canonicalized separately.
    public static func normalizePackageIdentifier(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        let normalized = trimmed.lowercased()
            .replacingOccurrences(of: "-", with: "_")
            .replacingOccurrences(of: " ", with: "_")
        let parts = normalized.split(separator: "_", omittingEmptySubsequences: false)
        guard !parts.isEmpty, parts.allSatisfy({ part in
            guard let first = part.first, first.isASCII, first.isLetter else { return false }
            return part.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber) }
        }) else { return "" }
        return normalized
    }

    /// Converts the snake_case names used in research JSON files into the stable
    /// Swift adapter identifiers. Unknown identifiers are preserved exactly
    /// (apart from whitespace normalization), so installing a future package
    /// never accidentally aliases it to an existing rule.
    public static func canonicalIdentifier(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        let compact = trimmed.lowercased().filter { $0.isLetter || $0.isNumber }
        switch compact {
        case "sweepreversalshort": return "sweepReversalShort"
        default: return trimmed
        }
    }

    private enum CodingKeys: String, CodingKey {
        case identifier, packageID, schemaVersion, version, displayName, englishName, runtimeHandler
        case sourceOfTruth, entryTimeframeMinutes, defaultParameters
        case defaultCooldownBars, scope
        case liveOrderMode, autoSubmitLiveOrders, enabledByDefault, lifecycle
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(identifier: try c.decode(String.self, forKey: .identifier),
                      version: try c.decode(String.self, forKey: .version),
                      displayName: try c.decode(String.self, forKey: .displayName),
                      englishName: try c.decodeIfPresent(String.self, forKey: .englishName) ?? "",
                      runtimeHandler: try c.decodeIfPresent(String.self, forKey: .runtimeHandler),
                      packageID: try c.decodeIfPresent(String.self, forKey: .packageID),
                      schemaVersion: try c.decodeIfPresent(Int.self, forKey: .schemaVersion),
                      sourceOfTruth: try c.decodeIfPresent(String.self, forKey: .sourceOfTruth),
                      entryTimeframeMinutes: try c.decodeIfPresent(Int.self, forKey: .entryTimeframeMinutes),
                      defaultParameters: try c.decodeIfPresent([String: Double].self, forKey: .defaultParameters) ?? [:],
                      defaultCooldownBars: try c.decodeIfPresent(Int.self, forKey: .defaultCooldownBars),
                      scope: try c.decodeIfPresent(String.self, forKey: .scope),
                      liveOrderMode: try c.decodeIfPresent(String.self, forKey: .liveOrderMode),
                      autoSubmitLiveOrders: try c.decodeIfPresent(Bool.self, forKey: .autoSubmitLiveOrders) ?? false,
                      enabledByDefault: try c.decodeIfPresent(Bool.self, forKey: .enabledByDefault) ?? false,
                      lifecycle: try c.decodeIfPresent(String.self, forKey: .lifecycle))
    }
}

public struct InstalledStrategyPackage: Codable, Equatable, Sendable, Identifiable {
    public let manifest: StrategyPackageManifest
    public let location: URL
    public var id: String { manifest.identifier }

    public init(manifest: StrategyPackageManifest, location: URL) {
        self.manifest = manifest
        self.location = location
    }
}

public enum StrategyPackageError: Error, LocalizedError, Equatable, Sendable {
    case invalidPackage(String)
    case invalidManifest(String)
    case alreadyInstalled(String)
    case notInstalled(String)
    case activePackage(String)
    case fileSystem(String)

    public var errorDescription: String? {
        switch self {
        case let .invalidPackage(message), let .invalidManifest(message), let .fileSystem(message): return message
        case let .alreadyInstalled(identifier): return "策略包已安装：\(identifier)"
        case let .notInstalled(identifier): return "策略包未安装：\(identifier)"
        case let .activePackage(identifier): return "策略包仍有运行中的策略实例，不能卸载：\(identifier)"
        }
    }
}

/// Actor isolated package store used by the local service and the strategy
/// laboratory integration.  Installation is atomic from the registry's
/// perspective: the package is copied to a temporary sibling, validated,
/// then moved into its final identifier directory.
public actor StrategyPackageRegistry {
    public let directory: URL
    private let fileManager: FileManager

    public init(directory: URL = StrategyPackageRegistry.defaultDirectory(), fileManager: FileManager = .default) {
        self.directory = directory
        self.fileManager = fileManager
    }

    public static func defaultDirectory(fileManager: FileManager = .default) -> URL {
        let base = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
        return base.appendingPathComponent("NovaTrade", isDirectory: true)
            .appendingPathComponent("strategy-packages", isDirectory: true)
    }

    /// Returns all valid installed packages. Invalid directories are ignored
    /// here so a broken import cannot take down the trading service; callers
    /// that need diagnostics should use `validate(package:)` first.
    public func installed() -> [InstalledStrategyPackage] {
        guard let entries = try? fileManager.contentsOfDirectory(at: directory,
                                                                  includingPropertiesForKeys: [.isDirectoryKey],
                                                                  options: [.skipsHiddenFiles]) else { return [] }
        return entries.compactMap { try? Self.readPackage(at: $0, fileManager: fileManager) }
            .sorted { $0.manifest.identifier < $1.manifest.identifier }
    }

    public func package(identifier: String) -> InstalledStrategyPackage? {
        let packageID = StrategyPackageManifest.normalizePackageIdentifier(identifier)
        let runtimeID = StrategyPackageManifest.canonicalIdentifier(identifier)
        return installed().first {
            $0.manifest.identifier == packageID ||
            StrategyPackageManifest.canonicalIdentifier($0.manifest.runtimeHandler) == runtimeID
        }
    }

    public func validate(package url: URL) throws -> StrategyPackageManifest {
        try Self.readManifest(at: url, fileManager: fileManager)
    }

    @discardableResult
    public func install(package url: URL, replacing: Bool = false) throws -> InstalledStrategyPackage {
        let manifest = try Self.readManifest(at: url, fileManager: fileManager)
        if let lifecycle = manifest.lifecycle, lifecycle != "finalized" {
            throw StrategyPackageError.invalidPackage("只有 lifecycle=finalized 的实验定稿包才能安装：\(manifest.identifier)")
        }
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent(manifest.identifier, isDirectory: true)
        let temporary = directory.appendingPathComponent(".install-\(UUID().uuidString)", isDirectory: true)
        let backup = directory.appendingPathComponent(".backup-\(UUID().uuidString)", isDirectory: true)
        do {
            if fileManager.fileExists(atPath: destination.path), !replacing {
                throw StrategyPackageError.alreadyInstalled(manifest.identifier)
            }
            if replacing, let current = try? Self.readPackage(at: destination, fileManager: fileManager),
               Self.compareVersions(manifest.version, current.manifest.version) < 0 {
                throw StrategyPackageError.invalidPackage("拒绝降级策略包 \(manifest.identifier)：已安装 \(current.manifest.version)，新包为 \(manifest.version)")
            }
            try fileManager.copyItem(at: url, to: temporary)
            _ = try Self.readManifest(at: temporary, fileManager: fileManager)
            if fileManager.fileExists(atPath: destination.path) {
                try fileManager.moveItem(at: destination, to: backup)
            }
            try fileManager.moveItem(at: temporary, to: destination)
            try? fileManager.removeItem(at: backup)
        } catch let error as StrategyPackageError {
            try? fileManager.removeItem(at: temporary)
            if fileManager.fileExists(atPath: backup.path), !fileManager.fileExists(atPath: destination.path) {
                try? fileManager.moveItem(at: backup, to: destination)
            }
            throw error
        } catch {
            try? fileManager.removeItem(at: temporary)
            if fileManager.fileExists(atPath: backup.path), !fileManager.fileExists(atPath: destination.path) {
                try? fileManager.moveItem(at: backup, to: destination)
            }
            throw StrategyPackageError.fileSystem(error.localizedDescription)
        }
        return try Self.readPackage(at: destination, fileManager: fileManager)
    }

    /// Remove an installed package. The registry itself does not own strategy
    /// instances, so the service should pass `canRemove: false` when an
    /// instance is running or otherwise references this identifier.
    @discardableResult
    public func uninstall(identifier: String, canRemove: Bool = true) throws -> InstalledStrategyPackage {
        let normalized = StrategyPackageManifest.normalizePackageIdentifier(identifier)
        guard let package = self.package(identifier: normalized) else {
            throw StrategyPackageError.notInstalled(normalized)
        }
        guard canRemove else { throw StrategyPackageError.activePackage(normalized) }
        do {
            try fileManager.removeItem(at: package.location)
        } catch {
            throw StrategyPackageError.fileSystem(error.localizedDescription)
        }
        return package
    }

    private static func readPackage(at url: URL, fileManager: FileManager) throws -> InstalledStrategyPackage {
        let manifest = try readManifest(at: url, fileManager: fileManager)
        return InstalledStrategyPackage(manifest: manifest, location: url)
    }

    private static func readManifest(at url: URL, fileManager: FileManager) throws -> StrategyPackageManifest {
        let rootValues = try? url.resourceValues(forKeys: [.isSymbolicLinkKey])
        guard rootValues?.isSymbolicLink != true else {
            throw StrategyPackageError.invalidPackage("策略包根目录不能是符号链接：\(url.lastPathComponent)")
        }
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory), isDirectory.boolValue else {
            throw StrategyPackageError.invalidPackage("策略包必须是目录：\(url.lastPathComponent)")
        }
        let candidates = [
            // Packed laboratory artifacts carry lifecycle/version metadata in
            // a root manifest. Research directories only have config/strategy.
            url.appendingPathComponent("manifest.json"),
            url.appendingPathComponent("config/strategy.json")
        ]
        guard let manifestURL = candidates.first(where: { fileManager.fileExists(atPath: $0.path) }) else {
            throw StrategyPackageError.invalidPackage("策略包缺少 config/strategy.json 或 manifest.json：\(url.lastPathComponent)")
        }
        // Do not let an imported package escape its root through a symlink.
        // FileManager.copyItem preserves symlinks, so this check must happen
        // before the copy and again on the temporary destination.
        if let enumerator = fileManager.enumerator(at: url, includingPropertiesForKeys: [.isSymbolicLinkKey], options: [.skipsHiddenFiles]) {
            for case let item as URL in enumerator {
                if (try? item.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
                    throw StrategyPackageError.invalidPackage("策略包不允许符号链接：\(item.lastPathComponent)")
                }
            }
        }
        do {
            let data = try Data(contentsOf: manifestURL)
            if manifestURL.lastPathComponent == "manifest.json",
               let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let lifecycle = root["lifecycle"] as? String,
               lifecycle != "finalized" {
                throw StrategyPackageError.invalidPackage("只有 lifecycle=finalized 的实验定稿包才能安装")
            }
            if manifestURL.lastPathComponent == "manifest.json",
               let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               root["artifacts"] != nil {
                try verifyArtifacts(root, at: url, fileManager: fileManager)
            }
            let manifest = try decodeManifest(data)
            let configURL = url.appendingPathComponent("config/strategy.json")
            guard manifestURL.lastPathComponent == "manifest.json",
                  fileManager.fileExists(atPath: configURL.path) else { return manifest }
            // The package manifest carries identity/lifecycle while the lab
            // config carries tuned signal and risk defaults.
            let config = try decodeManifest(Data(contentsOf: configURL))
            return try merge(manifest, with: config)
        } catch let error as StrategyPackageError {
            throw error
        } catch {
            throw StrategyPackageError.invalidManifest("无法读取策略包配置：\(error.localizedDescription)")
        }
    }

    /// Parses both the compact registry manifest and the richer experiment
    /// `config/strategy.json` files already present in this repository.
    public static func decodeManifest(_ data: Data) throws -> StrategyPackageManifest {
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw StrategyPackageError.invalidManifest("配置根节点必须是 JSON 对象")
        }
        let runtime = object["runtime"] as? [String: Any] ?? object["runtime_integration"] as? [String: Any] ?? [:]
        let position = object["position_management"] as? [String: Any]
            ?? object["risk_policy"] as? [String: Any] ?? object["portfolio"] as? [String: Any] ?? [:]
        let signal = object["signal_parameters"] as? [String: Any]
            ?? object["parameters"] as? [String: Any] ?? [:]
        let identifier = string(object["strategy_id"])
            ?? string(object["package_id"])
            ?? string(object["identifier"])
            ?? string(object["id"])
            ?? string(object["name"])
            ?? string(object["strategy"])
            ?? string(runtime["strategy_id"])
            ?? string(runtime["strategy_type"])
        guard let identifier else { throw StrategyPackageError.invalidManifest("缺少 strategy_type 或 identifier") }
        let display = string(object["display_name"])
            ?? string(object["name_zh"])
            ?? string(object["name"])
            ?? identifier
        let english = string(object["name_en"]) ?? display
        let version = string(object["version"]) ?? "0.0.0"
        let handler = string(runtime["strategy_type"])
            ?? string(runtime["runtime_handler"])
            ?? string(object["runtime_handler"])
            ?? identifier
        let source = string(object["source_of_truth"])
        let entry = integer(object["entry_timeframe_minutes"])
            ?? integer(runtime["entry_timeframe_minutes"])
        var defaults = signal.reduce(into: [String: Double]()) { result, pair in
            if let number = number(pair.value) { result[camelCase(pair.key)] = number }
        }
        // Leverage is an execution/position setting rather than a signal
        // parameter in laboratory configs. Keep it in the same numeric
        // parameter bag used by StrategyConfig so package-created instances
        // receive the package's declared default instead of silently falling
        // back to the compiled strategy default.
        if let leverage = number(position["leverage"]) {
            defaults["leverage"] = leverage
        }
        let cooldown = integer(position["cooldown_bars"])
        let mode = string(runtime["live_order_mode"])
            ?? string(object["live_order_mode"])
        let autoSubmit = bool(runtime["auto_submit_live_orders"])
            ?? bool(object["auto_submit_live_orders"]) ?? false
        let enabled = bool(runtime["enabled_by_default"])
            ?? bool(object["enabled_by_default"]) ?? false
        let scope = string(runtime["scope"])
        let packageID = string(object["package_id"])
        let schemaVersion = integer(object["schema_version"])
        let lifecycleCandidate = string(object["lifecycle"]) ?? string(object["status"])
        let lifecycle = ["draft", "candidate", "finalized", "retired"].contains(lifecycleCandidate ?? "")
            ? lifecycleCandidate : nil
        return try StrategyPackageManifest(identifier: identifier,
                                            version: version,
                                            displayName: display,
                                            englishName: english,
                                            runtimeHandler: handler,
                                            packageID: packageID,
                                            schemaVersion: schemaVersion,
                                            sourceOfTruth: source,
                                            entryTimeframeMinutes: entry,
                                            defaultParameters: defaults,
                                            defaultCooldownBars: cooldown,
                                            scope: scope,
                                            liveOrderMode: mode,
                                            autoSubmitLiveOrders: autoSubmit,
                                            enabledByDefault: enabled,
                                            lifecycle: lifecycle)
    }

    private static func merge(_ package: StrategyPackageManifest,
                              with config: StrategyPackageManifest) throws -> StrategyPackageManifest {
        guard StrategyPackageManifest.canonicalIdentifier(package.runtimeHandler) ==
              StrategyPackageManifest.canonicalIdentifier(config.runtimeHandler) else {
            throw StrategyPackageError.invalidManifest("manifest.json 与 config/strategy.json 的运行时标识不一致")
        }
        var defaults = config.defaultParameters
        // Keep position/execution defaults from the laboratory config even
        // when the root manifest also provides signal parameters. Package
        // manifest values remain authoritative for keys they explicitly set.
        for (key, value) in package.defaultParameters { defaults[key] = value }
        return try StrategyPackageManifest(identifier: package.identifier,
                                    version: package.version,
                                    displayName: package.displayName,
                                    englishName: package.englishName,
                                    runtimeHandler: package.runtimeHandler,
                                    packageID: package.packageID,
                                    schemaVersion: package.schemaVersion,
                                    sourceOfTruth: package.sourceOfTruth ?? config.sourceOfTruth,
                                    entryTimeframeMinutes: package.entryTimeframeMinutes ?? config.entryTimeframeMinutes,
                                    defaultParameters: defaults,
                                    defaultCooldownBars: package.defaultCooldownBars ?? config.defaultCooldownBars,
                                    scope: package.scope ?? config.scope,
                                    liveOrderMode: package.liveOrderMode ?? config.liveOrderMode,
                                    // Keep legacy execution metadata for
                                    // decoding and catalog display only. The
                                    // runtime chooses paper/live from the
                                    // authenticated account mode; a package
                                    // must not opt in to or forbid either mode.
                                    autoSubmitLiveOrders: package.autoSubmitLiveOrders,
                                    enabledByDefault: package.enabledByDefault,
                                    lifecycle: package.lifecycle)
    }

    private static func verifyArtifacts(_ manifest: [String: Any], at root: URL,
                                        fileManager: FileManager) throws {
        guard let artifacts = manifest["artifacts"] as? [[String: Any]], !artifacts.isEmpty else {
            throw StrategyPackageError.invalidManifest("artifacts 必须是非空数组")
        }
        let rootPath = root.standardizedFileURL.path
        for artifact in artifacts {
            guard let rawPath = artifact["path"] as? String,
                  let expected = artifact["sha256"] as? String,
                  expected.range(of: "^[0-9a-fA-F]{64}$", options: .regularExpression) != nil else {
                throw StrategyPackageError.invalidManifest("artifact 必须包含 64 位 sha256 和相对路径")
            }
            let relative = rawPath.trimmingCharacters(in: .whitespacesAndNewlines)
            let candidate = root.appendingPathComponent(relative).standardizedFileURL
            let prefix = rootPath.hasSuffix("/") ? rootPath : rootPath + "/"
            guard !relative.isEmpty, !relative.contains("\\"), candidate.path.hasPrefix(prefix),
                  fileManager.fileExists(atPath: candidate.path) else {
                throw StrategyPackageError.invalidPackage("artifact 路径非法或不是普通文件：\(rawPath)")
            }
            let values = try candidate.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            guard values.isDirectory != true, values.isSymbolicLink != true else {
                throw StrategyPackageError.invalidPackage("artifact 路径非法或不是普通文件：\(rawPath)")
            }
            let digest = SHA256.hash(data: try Data(contentsOf: candidate)).map { String(format: "%02x", $0) }.joined()
            guard digest.caseInsensitiveCompare(expected) == .orderedSame else {
                throw StrategyPackageError.invalidPackage("artifact 校验失败：\(rawPath)")
            }
        }
    }

    private static func string(_ value: Any?) -> String? {
        guard let value else { return nil }
        if let value = value as? String { return value }
        return nil
    }

    private static func number(_ value: Any?) -> Double? {
        guard let value else { return nil }
        if let value = value as? NSNumber { return value.doubleValue.isFinite ? value.doubleValue : nil }
        if let value = value as? String, let parsed = Double(value), parsed.isFinite { return parsed }
        return nil
    }

    private static func integer(_ value: Any?) -> Int? {
        guard let number = number(value) else { return nil }
        return Int(number.rounded())
    }

    private static func bool(_ value: Any?) -> Bool? {
        guard let value else { return nil }
        if let value = value as? Bool { return value }
        if let value = value as? NSNumber { return value.boolValue }
        if let value = value as? String { return Bool(value.lowercased()) }
        return nil
    }

    private static func camelCase(_ key: String) -> String {
        guard key.contains("_") else { return key }
        let pieces = key.split(separator: "_")
        guard let first = pieces.first else { return key }
        return String(first) + pieces.dropFirst().map { $0.prefix(1).uppercased() + $0.dropFirst() }.joined()
    }

    /// Compare the numeric components and prerelease suffixes used by the
    /// laboratory package tool.  A malformed installed version is kept
    /// installable, but never treated as newer than a valid incoming version.
    private static func compareVersions(_ lhs: String, _ rhs: String) -> Int {
        func parse(_ value: String) -> ([Int], [String]) {
            let coreAndPre = value.split(separator: "+", maxSplits: 1, omittingEmptySubsequences: false)[0]
            let pieces = coreAndPre.split(separator: "-", maxSplits: 1, omittingEmptySubsequences: false)
            let numbers = pieces[0].split(separator: ".").map { Int($0) ?? 0 }
            let pre = pieces.count > 1 ? pieces[1].split(separator: ".").map(String.init) : []
            return (numbers + Array(repeating: 0, count: max(0, 3 - numbers.count)), pre)
        }
        let left = parse(lhs), right = parse(rhs)
        let numeric = zip(left.0, right.0).first(where: { $0.0 != $0.1 })
        if let numeric { return numeric.0 < numeric.1 ? -1 : 1 }
        if left.1.isEmpty != right.1.isEmpty { return left.1.isEmpty ? 1 : -1 }
        for (a, b) in zip(left.1, right.1) where a != b {
            if let ai = Int(a), let bi = Int(b) { return ai < bi ? -1 : 1 }
            if let _ = Int(a) { return -1 }
            if let _ = Int(b) { return 1 }
            return a < b ? -1 : 1
        }
        return left.1.count == right.1.count ? 0 : (left.1.count < right.1.count ? -1 : 1)
    }
}
