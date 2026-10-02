import SwiftUI
import TradingDomain

struct RightRail: View {
    @ObservedObject var model: DashboardModel
    @Binding var showingNewStrategy: Bool

    private var accountLabel: String { model.accountOverview.mode == .paper ? "OKX 模拟" : "实盘" }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Label("策略状态", systemImage: "brain.head.profile")
                        .font(.headline)
                    Spacer()
                    Button { showingNewStrategy = true } label: { Image(systemName: "plus") }
                        .buttonStyle(.bordered).controlSize(.small).help("新建策略")
                }
                .padding(.vertical, 4)
                if model.riskSnapshot.killSwitch {
                    RiskAlertModule(model: model)
                }
                ForEach(model.strategies) { config in
                    StrategyStatusModule(config: config, status: model.strategyStatuses.first { $0.id == config.id }, model: model)
                }
                RailModule(title: "持仓", icon: "chart.bar.xaxis") {
                    if model.livePositions.isEmpty { RailEmpty("暂无\(accountLabel)持仓") }
                    ForEach(model.livePositions) { position in
                        let pnl = position.unrealizedPnL ?? 0
                        RailRow {
                            Text(position.instrumentID).font(.caption.monospaced())
                            Spacer()
                            Text(formatQuantity(position.quantity)).font(.caption.monospacedDigit())
                            Text(position.side.uppercased()).foregroundStyle(.secondary)
                            Text(formatSigned(pnl)).foregroundStyle(pnl >= 0 ? .green : .red)
                        }
                    }
                }
                RailModule(title: "挂单", icon: "list.bullet.rectangle") {
                    if model.liveOrders.isEmpty { RailEmpty("暂无\(accountLabel)挂单") }
                    ForEach(model.liveOrders) { order in
                        RailRow {
                            Text(order.instrumentID).font(.caption.monospaced())
                            Spacer()
                            Text(order.side.uppercased()).font(.caption2.weight(.semibold))
                            Text(order.status).foregroundStyle(.secondary)
                        }
                    }
                }
                RailModule(title: "交易流水", icon: "arrow.left.arrow.right") {
                    if model.fills.isEmpty { RailEmpty("暂无\(accountLabel)成交记录") }
                    ForEach(model.fills.prefix(8)) { fill in
                        RailRow {
                            Text(fill.timestamp.formatted(date: .omitted, time: .shortened)).foregroundStyle(.secondary)
                            Spacer()
                            Text(formatQuantity(fill.quantity)).monospacedDigit()
                            Text("费 \(formatQuantity(fill.fee))").foregroundStyle(.secondary)
                        }
                    }
                }
                RailModule(title: "运行日志", icon: "text.alignleft") {
                    if model.runtimeLogs.isEmpty { RailEmpty("等待服务事件…") }
                    ForEach(model.runtimeLogs.suffix(12).reversed()) { log in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(log.message).font(.caption).lineLimit(2)
                            Text(log.timestamp.formatted(date: .omitted, time: .shortened)).font(.caption2).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
                    }
                }
            }.padding(16)
        }
        .frame(width: 350)
        .background(Color.sidebarBackground.opacity(0.7))
    }
}

/// The account level kill switch is separate from each strategy's paused or
/// running state. Keep it visible above the strategy cards so a rejected start
/// has an actionable explanation and a reset entry point.
struct RiskAlertModule: View {
    @ObservedObject var model: DashboardModel
    @State private var resetFeedback: String?
    @State private var resetFeedbackIsError = false

    private var risk: RiskSnapshot { model.riskSnapshot }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Label("账户风控已熔断", systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.orange)
                Spacer(minLength: 0)
                Text(risk.reason ?? "风险熔断")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.orange)
                    .lineLimit(1)
            }

            HStack(spacing: 12) {
                riskMetric("单日损益", value: formatSignedPercent(risk.dailyPnLPercent.doubleValue), color: risk.dailyPnLPercent < 0 ? .red : .green)
                riskMetric("累计回撤", value: String(format: "%.2f%%", risk.drawdownPercent.doubleValue), color: .orange)
                riskMetric("账户权益", value: formatUSD(risk.equity), color: .primary)
            }

            Text("策略启动已暂停。复位仅在 UTC 新自然日后生效。")
                .font(.caption2)
                .foregroundStyle(.secondary)

            if let resetFeedback {
                Text(resetFeedback)
                    .font(.caption2)
                    .foregroundStyle(resetFeedbackIsError ? .red : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Button(action: reset) {
                HStack(spacing: 6) {
                    if model.isResettingRisk {
                        ProgressView().controlSize(.small)
                        Text("复位中…")
                    } else {
                        Image(systemName: "arrow.clockwise")
                        Text("复位风控")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .tint(.orange)
            .disabled(model.isResettingRisk)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.orange.opacity(0.35)))
    }

    private func reset() {
        resetFeedback = nil
        resetFeedbackIsError = false
        Task {
            do {
                let result = try await model.resetRisk()
                resetFeedback = result.killSwitch ? "风控仍锁存：只能在 UTC 新自然日后复位。" : "账户风控已复位。"
            } catch {
                resetFeedbackIsError = true
                resetFeedback = "复位失败：\(error.localizedDescription)"
            }
        }
    }

    private func riskMetric(_ title: String, value: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.caption.monospacedDigit().weight(.semibold)).foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct StrategyStatusModule: View {
    let config: StrategyConfig
    let status: StrategyStatus?
    @ObservedObject var model: DashboardModel
    @State private var showingDeleteConfirmation = false
    @State private var showingPositionDeleteConfirmation = false

    private var isRunning: Bool { config.enabled }
    private var stateColor: Color { isRunning ? .green : .orange }
    private var openPositions: [PositionSnapshot] { model.strategyOpenPositions(for: config) }

    private var stopLossText: String {
        guard let signal = status?.lastSignal, let stop = signal.stopPrice, signal.price != 0 else {
            return "策略止损：\(config.type.stopLossDescription)"
        }
        let distance = abs(stop.doubleValue - signal.price.doubleValue)
        return String(format: "策略止损 %.2f%%（动态）", distance / abs(signal.price.doubleValue) * 100)
    }

    var body: some View {
        let pnl = status?.pnl ?? 0
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 8) {
                Text(config.name).font(.subheadline.weight(.semibold)).lineLimit(1)
                Spacer()
                Button { requestDelete() } label: { Image(systemName: "trash") }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .opacity(isRunning ? 0.45 : 1)
                    .disabled(isRunning)
                    .help(isRunning ? "请先停止策略" : "删除策略实例")
                Text(status?.direction?.uppercased() ?? "空仓").font(.caption2.weight(.semibold)).padding(.horizontal, 7).padding(.vertical, 4).background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 5))
            }
            HStack(spacing: 6) {
                Text(config.scope.displayName).font(.caption.monospaced()).foregroundStyle(.secondary)
                Spacer()
                Circle().fill(stateColor).frame(width: 7, height: 7)
                Text(isRunning ? "运行中" : "已暂停").font(.caption2).foregroundStyle(stateColor)
            }
            if let signal = status?.lastSignal {
                Text("最近信号 \(signal.type) @ \(formatPrice(signal.price))").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            } else {
                Text("暂无最近信号").font(.caption2).foregroundStyle(.secondary)
            }
            Text(stopLossText).font(.caption2).foregroundStyle(.secondary)
            HStack {
                Text("收益 \(formatSigned(pnl)) USDT").font(.caption2.monospacedDigit()).foregroundStyle(pnl >= 0 ? .green : .red)
                Spacer()
                Button(isRunning ? "暂停" : "启动") { model.toggleStrategy(config.id) }.buttonStyle(.bordered).controlSize(.mini)
            }
        }
        .padding(12)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(isRunning ? Color.mint.opacity(0.25) : Color.white.opacity(0.07)))
        .confirmationDialog("删除策略实例？", isPresented: $showingDeleteConfirmation, titleVisibility: .visible) {
            Button("删除", role: .destructive) { model.deleteStrategy(config, closePositions: false) }
            Button("取消", role: .cancel) {}
        } message: {
            Text("删除后不会再自动运行该规则，但历史挂单和成交记录会保留。")
        }
        .confirmationDialog("策略存在未平仓位", isPresented: $showingPositionDeleteConfirmation, titleVisibility: .visible) {
            Button("平仓并删除", role: .destructive) { model.deleteStrategy(config, closePositions: true) }
            Button("取消", role: .cancel) {}
        } message: {
            Text("检测到 \(openPositions.count) 个未平仓位。必须先平仓并确认远端持仓归零后才能删除策略。")
        }
    }

    private func requestDelete() {
        guard !isRunning else {
            model.errorMessage = "策略运行中，请先停止策略后再删除"
            return
        }
        if openPositions.isEmpty {
            showingDeleteConfirmation = true
        } else {
            showingPositionDeleteConfirmation = true
        }
    }
}

struct RailModule<Content: View>: View {
    let title: String
    let icon: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: icon).font(.headline)
            content()
        }.padding(12).frame(maxWidth: .infinity, alignment: .leading).background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 8))
    }
}

struct RailRow<Content: View>: View {
    @ViewBuilder let content: () -> Content

    var body: some View { HStack(spacing: 6) { content() }.font(.caption2).padding(.vertical, 3) }
}

struct RailEmpty: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View { Text(text).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 34) }
}
