import AppKit
import SwiftUI

/// Loads exchange symbols without blocking the sidebar. The on-disk cache is
/// deliberately scoped to the app's caches directory so stale artwork can be
/// discarded by macOS without affecting trading state.
@MainActor
final class ContractIconLoader: ObservableObject {
    @Published private(set) var image: NSImage?
    @Published private(set) var isLoading = false

    private static let memoryCache = NSCache<NSString, NSImage>()
    private var symbol: String
    private var generation = UUID()

    init(symbol: String) {
        self.symbol = symbol
    }

    /// A view can keep its identity while its selected contract changes. Drop
    /// the previous image so the new symbol cannot inherit stale artwork.
    func updateSymbol(_ symbol: String) {
        guard self.symbol != symbol else { return }
        self.symbol = symbol
        generation = UUID()
        image = nil
        isLoading = false
    }

    func load() async {
        guard image == nil, !isLoading else { return }
        let requestSymbol = symbol
        let requestGeneration = generation
        let candidates = iconCandidates(for: requestSymbol)
        guard !candidates.isEmpty else { return }
        isLoading = true
        defer {
            if requestGeneration == generation { isLoading = false }
        }

        for candidate in candidates {
            guard requestGeneration == generation else { return }
            let key = candidate.absoluteString as NSString
            if let cached = Self.memoryCache.object(forKey: key) {
                image = cached
                return
            }
            if let cached = readDiskCache(for: candidate) {
                Self.memoryCache.setObject(cached, forKey: key)
                image = cached
                return
            }

            do {
                let request = URLRequest(url: candidate, cachePolicy: .returnCacheDataElseLoad, timeoutInterval: 8)
                let (data, response) = try await URLSession.shared.data(for: request)
                guard requestGeneration == generation else { return }
                guard let http = response as? HTTPURLResponse,
                      (200..<300).contains(http.statusCode),
                      http.mimeType?.hasPrefix("image/") == true,
                      let decoded = NSImage(data: data), decoded.isValid else { continue }
                Self.memoryCache.setObject(decoded, forKey: key)
                writeDiskCache(data, for: candidate)
                image = decoded
                return
            } catch {
                // Try the next known public icon key. A missing icon should
                // never prevent contracts and prices from rendering.
            }
        }
    }

    private func iconCandidates(for rawSymbol: String) -> [URL] {
        let normalized = rawSymbol
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .filter { $0.isLetter || $0.isNumber }
        guard !normalized.isEmpty else { return [] }

        // OKX occasionally prefixes a contract ticker with its lot multiplier
        // (for example 1000SHIB). CoinCap indexes the underlying asset.
        var tickers = [normalized]
        if normalized.hasPrefix("1000"), normalized.count > 4 {
            tickers.append(String(normalized.dropFirst(4)))
        }
        if normalized.hasPrefix("1000000"), normalized.count > 7 {
            tickers.append(String(normalized.dropFirst(7)))
        }
        // OKX is the source of truth for the symbols shown in this app and
        // includes recently listed contracts that CoinCap does not know yet.
        // Keep CoinCap as a public fallback for any temporary OKX CDN miss.
        let okx = tickers.compactMap { URL(string: "https://www.okx.com/cdn/oksupport/asset/currency/icon/\($0).png") }
        let coincap = tickers.compactMap { URL(string: "https://assets.coincap.io/assets/icons/\($0)@2x.png") }
        return okx + coincap
    }

    private var diskDirectory: URL? {
        guard let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first else { return nil }
        let directory = caches.appendingPathComponent("NovaTrade/contract-icons", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func cacheFile(for url: URL) -> URL? {
        diskDirectory?.appendingPathComponent(url.lastPathComponent.replacingOccurrences(of: "@", with: "_"))
    }

    private func readDiskCache(for url: URL) -> NSImage? {
        guard let file = cacheFile(for: url),
              let data = try? Data(contentsOf: file),
              let cached = NSImage(data: data), cached.isValid else { return nil }
        return cached
    }

    private func writeDiskCache(_ data: Data, for url: URL) {
        guard let file = cacheFile(for: url) else { return }
        try? data.write(to: file, options: .atomic)
    }
}

struct ContractIcon: View {
    let symbol: String
    let size: CGFloat
    @StateObject private var loader: ContractIconLoader

    init(symbol: String, size: CGFloat = 24) {
        self.symbol = symbol
        self.size = size
        _loader = StateObject(wrappedValue: ContractIconLoader(symbol: symbol))
    }

    var body: some View {
        RoundedRectangle(cornerRadius: 5)
            .fill(loader.image == nil ? fallbackColor.opacity(0.18) : Color.secondary.opacity(0.14))
            .frame(width: size, height: size)
            .overlay {
                if let image = loader.image {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFit()
                        .padding(3)
                } else if loader.isLoading {
                    ProgressView().controlSize(.mini)
                } else {
                    Text(fallbackLabel)
                        .font(.system(size: max(9, size * 0.38), weight: .bold, design: .rounded))
                        .minimumScaleFactor(0.55)
                        .lineLimit(1)
                        .foregroundStyle(fallbackColor)
                }
            }
            .task(id: symbol) {
                loader.updateSymbol(symbol)
                await loader.load()
            }
            .onChange(of: symbol) { _, newSymbol in
                loader.updateSymbol(newSymbol)
            }
            .accessibilityLabel("\(symbol) 图标")
    }

    private var fallbackLabel: String {
        let normalized = symbol.uppercased().filter { $0.isLetter || $0.isNumber }
        let base: String
        if normalized.hasPrefix("1000000"), normalized.count > 7 {
            base = String(normalized.dropFirst(7))
        } else if normalized.hasPrefix("1000"), normalized.count > 4 {
            base = String(normalized.dropFirst(4))
        } else {
            base = normalized
        }
        return String(base.prefix(3))
    }

    private var fallbackColor: Color {
        let palette: [Color] = [.orange, .indigo, .purple, .blue, .yellow, .teal, .pink, .cyan]
        let seed = symbol.uppercased().unicodeScalars.reduce(0) { $0 + Int($1.value) }
        return palette[seed % palette.count]
    }
}
