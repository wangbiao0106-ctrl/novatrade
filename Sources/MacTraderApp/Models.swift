import SwiftUI
import TradingDomain

/// A sidebar row for one OKX USDT perpetual swap.
struct PerpetualContract: Identifiable, Hashable {
    let id: String
    let name: String
    let shortName: String
    let quoteCurrency: String
    let price: Double
    /// UTC-day move used by the regular market display.
    let change: Double
    /// Rolling 24h move retained for strategy filters and diagnostics.
    let rollingChange: Double
    /// Rolling 24h quote turnover.
    let volume24h: Double
    let category: String

    var pairLabel: String { "\(shortName) / \(quoteCurrency)" }

    /// Derived from the symbol's characters so a coin keeps its colour
    /// across launches (`String.hashValue` is randomly seeded per process).
    var accent: Color {
        let palette: [Color] = [.orange, .indigo, .purple, .blue, .yellow, .teal, .pink, .cyan]
        let seed = shortName.unicodeScalars.reduce(0) { $0 + Int($1.value) }
        return palette[seed % palette.count]
    }

    init(remote: ContractMarket) {
        id = remote.id
        name = remote.name
        shortName = remote.baseCurrency
        quoteCurrency = remote.quoteCurrency
        price = remote.last.doubleValue
        change = remote.changePercent.doubleValue
        rollingChange = remote.rollingChangePercent.doubleValue
        volume24h = remote.volume24h.doubleValue
        category = remote.category
    }
}

/// Sidebar contract groupings, ranked from OKX's UTC-day figures (`sodUtc0`).
enum MarketCategory: String, CaseIterable {
    case mainstream = "主流"
    case hot = "热门"
    case gainers = "涨幅"
    case losers = "跌幅"
}

enum BackendServiceState {
    case stopped, starting, running, stopping, unavailable

    var title: String {
        switch self {
        case .stopped: return "后台已停止"
        case .starting: return "后台启动中"
        case .running: return "后台已连接"
        case .stopping: return "后台停止中"
        case .unavailable: return "后台不可用"
        }
    }

    var color: Color {
        switch self {
        case .running: return .green
        case .starting, .stopping: return .orange
        case .stopped: return .secondary
        case .unavailable: return .red
        }
    }
}

enum CandleDataSource: Equatable {
    case connecting
    /// The socket is subscribed but no candle has arrived on this
    /// subscription yet, which is the normal state on a quiet market.
    case subscribed
    case websocket
    case reconnecting
    case stopped
    case unavailable

    var label: String {
        switch self {
        case .connecting: return "正在连接 OKX WSS"
        case .subscribed: return "OKX WSS 已订阅 · 等待推送"
        case .websocket: return "OKX WSS 长连接 · 实时推送"
        case .reconnecting: return "WSS 已断开 · 自动重连中"
        case .stopped: return "后台服务已停止"
        case .unavailable: return "等待行情连接"
        }
    }

    var icon: String {
        switch self {
        case .connecting: return "antenna.radiowaves.left.and.right"
        case .subscribed: return "dot.radiowaves.left.and.right"
        case .websocket: return "bolt.horizontal.circle.fill"
        case .reconnecting: return "arrow.triangle.2.circlepath"
        case .stopped: return "pause.circle"
        case .unavailable: return "clock.arrow.circlepath"
        }
    }

    /// True while the WSS subscription is up. Both states mean the connection
    /// is established; they differ only in whether a bar has arrived yet.
    var isLive: Bool { self == .websocket || self == .subscribed }
}
