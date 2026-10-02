import SwiftUI
import TradingDomain

struct DashboardWindowHeader: View {
    @ObservedObject var model: DashboardModel

    var body: some View {
        HStack(spacing: 0) {
            AccountToolbarLabel(model: model)
            Spacer(minLength: 20)
            BackendToolbarControls(model: model)
        }
        .padding(.horizontal, 18)
        .frame(maxWidth: .infinity, minHeight: 52, maxHeight: 52, alignment: .leading)
        .background(Color.panelBackground)
        .overlay(alignment: .bottom) {
            Divider().overlay(Color.white.opacity(0.1))
        }
    }
}

struct AccountToolbarLabel: View {
    @ObservedObject var model: DashboardModel

    private var accountName: String {
        if let label = model.accountOverview.label?.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty {
            return label
        }
        switch model.accountOverview.mode {
        case .paper: return "模拟1"
        case .live: return "实盘账户"
        case .readOnly: return "账户"
        }
    }

    var body: some View {
        Label(accountName, systemImage: model.accountOverview.mode == .live ? "bolt.shield.fill" : "person.crop.circle.fill")
            .font(.subheadline.weight(.semibold))
            .labelStyle(.titleAndIcon)
            .foregroundStyle(model.accountOverview.mode == .live ? Color.orange : Color.mint)
            .lineLimit(1)
            .help(accountName)
            .accessibilityLabel("账户：\(accountName)")
    }
}

struct BackendToolbarControls: View {
    @ObservedObject var model: DashboardModel

    private var canStop: Bool {
        model.serviceState == .running || model.serviceState == .starting
    }

    var body: some View {
        HStack(spacing: 10) {
            HStack(spacing: 6) {
                Circle()
                    .fill(model.serviceState.color)
                    .frame(width: 7, height: 7)
                    .accessibilityHidden(true)
                Text(model.serviceState.title)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(model.serviceState.color)
            }
            Button(canStop ? "停止" : "启动") {
                model.toggleBackend()
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(model.serviceState == .stopping)
            .help(canStop ? "停止后台服务" : "启动后台服务")
            .accessibilityLabel(canStop ? "停止后台服务" : "启动后台服务")
        }
        .fixedSize(horizontal: true, vertical: false)
    }
}
