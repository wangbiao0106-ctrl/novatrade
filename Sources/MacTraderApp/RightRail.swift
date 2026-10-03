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
                            Text(formatLocalTime(fill.timestamp)).foregroundStyle(.secondary)
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
                            Text(formatLocalTime(log.timestamp)).font(.caption2).foregroundStyle(.secondary)
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
    private var capital: StrategyCapitalSnapshot? { model.strategyCapital(for: config) }
    private var universe: StrategyUniverseSnapshot? { model.strategyUniverse(for: config) }
    private var maxConcurrentPositions: Int { Int(config.parameters["maxConcurrentPositions"] ?? 1) }
    private var directionLabel: String {
        if let direction = status?.direction, let label = directionText(direction) {
            return label
        }
        if let signal = status?.lastSignal, let label = directionText(signal.type) {
            return label
        }
        switch config.type {
        case .sweepReversalShort:
            return "做空"
        case .external:
            return "方向未知"
        }
    }
    private var directionColor: Color {
        switch directionLabel {
        case "做多": return .orange
        case "做空": return .mint
        default: return .secondary
        }
    }
    /// A tripped account breaker stops every strategy; starting one again is
    /// refused by the service until the breaker is reset.
    private var startBlockedByKillSwitch: Bool { !isRunning && model.riskSnapshot.killSwitch }

    /// One line of fixed facts: scope, signal cycle and the size of the pool
    /// the scanner resolved. The full rule definition is the tooltip.
    private var metaText: String {
        let scope: String
        if case .dynamicCategory = config.scope.mode {
            scope = config.scope.category?.shortName ?? "未分类"
        } else {
            scope = config.scope.displayName
        }
        var parts = [scope, config.type.signalCycleShortLabel]
        if let universe {
            // Zero is a normal outcome on a quiet day: no symbol clears the
            // rule's gain and turnover gates. Say so instead of "scanning 0".
            parts.append(universe.targetCount == 0 ? "暂无符合条件的币" : "扫描 \(universe.targetCount) 个币")
        }
        return parts.joined(separator: " · ")
    }

    private var ruleDetails: String {
        [
            "规则：\(config.type.displayName)",
            "版本：\(config.strategyVersion.map { "v\($0)" } ?? "未发布")",
            "信号：\(config.type.signalCycleDescription)",
            "止损：\(config.type.stopLossDescription)",
            "止盈：\(config.type.takeProfitDescription)",
            "冷却：\(config.type.cooldownDescription)",
            "候选范围：\(config.scope.displayName)"
        ].joined(separator: "\n")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            Text(metaText)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .help(ruleDetails)
            divider
            capitalMetrics
            pnlRow
            divider
            signalRows
            if !openPositions.isEmpty {
                divider
                positionRows
            }
            footer
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

    private var divider: some View { Divider().overlay(Color.white.opacity(0.08)) }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(config.name)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Spacer(minLength: 4)
                HStack(spacing: 5) {
                    Circle().fill(stateColor).frame(width: 7, height: 7)
                    Text(isRunning ? "运行中" : "已暂停").font(.caption2).foregroundStyle(stateColor)
                }
                .fixedSize(horizontal: true, vertical: false)
                Menu {
                    Button(role: .destructive) { requestDelete() } label: {
                        Label("删除策略实例", systemImage: "trash")
                    }
                    .disabled(isRunning)
                } label: {
                    Image(systemName: "ellipsis.circle").foregroundStyle(.secondary)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help(isRunning ? "运行中的策略不能删除，请先暂停" : "更多操作")
                .accessibilityLabel("策略操作")
            }
            HStack(spacing: 6) {
                strategyTag(directionLabel, color: directionColor)
                strategyTag("\(formatLeverage(config.leverage))x", color: .secondary)
                Spacer(minLength: 0)
            }
        }
    }

    private var capitalMetrics: some View {
        HStack(alignment: .top, spacing: 8) {
            metric("资金池", value: capital.map { formatUSD($0.equity) } ?? "--")
            metric("持仓占用", value: capital.map { formatUSD($0.reservedCapital) } ?? "--")
            metric("持仓", value: "\(openPositions.count) / \(maxConcurrentPositions)")
        }
    }

    private func strategyTag(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(color.opacity(0.12), in: Capsule())
            .fixedSize(horizontal: true, vertical: false)
    }

    private func metric(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.caption2.monospacedDigit().weight(.semibold)).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Strategy results live in the capital pool, which the service settles
    /// from exchange history. `StrategyStatus.pnl` is never written on the
    /// exchange path and would always read zero here.
    private var pnlRow: some View {
        let realized = capital?.realizedPnL ?? 0
        let unrealized = capital?.unrealizedPnL ?? 0
        let total = realized + unrealized
        let totalColor: Color = {
            guard capital != nil else { return .secondary }
            if total > 0 { return .green }
            if total < 0 { return .red }
            return .secondary
        }()
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text("收益 \(formatSigned(total)) USDT")
                    .font(.caption2.monospacedDigit().weight(.semibold))
                    .foregroundStyle(totalColor)
                Spacer(minLength: 0)
            }
            HStack(spacing: 12) {
                Text("已实现 \(formatSigned(realized))")
                Text("浮动 \(formatSigned(unrealized))")
                Spacer(minLength: 0)
            }
            .font(.caption2.monospacedDigit())
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .minimumScaleFactor(0.8)
        }
    }

    @ViewBuilder
    private var signalRows: some View {
        if let signal = status?.lastSignal {
            // A signal carries no instrument; the entry it produced does.
            let order = model.strategyOrder(for: signal)
            HStack(spacing: 4) {
                Text("最近信号").foregroundStyle(.secondary)
                Text(signalDirectionText(signal.type)).fontWeight(.semibold)
                if let order { Text(order.instrumentID).monospaced() }
                Text("· \(formatShortTimestamp(signal.timestamp)) · @ \(formatPrice(signal.price))").foregroundStyle(.secondary)
                if order == nil { Text("未下单").foregroundStyle(.orange) }
            }
            .font(.caption2)
            .lineLimit(1)
            .help(signal.reason)
        } else {
            Text("最近信号 暂无").font(.caption2).foregroundStyle(.secondary)
        }
        if let status, status.cooldown > 0 {
            let interval = config.type.cooldownBarInterval
            let minutes = status.cooldown * interval.minutes
            Label("冷却中 · 剩余 \(status.cooldown) 根 \(interval.displayName) K 线 · 约 \(TradingDurationText.describe(minutes: minutes))", systemImage: "hourglass")
                .font(.caption2)
                .foregroundStyle(.orange)
                .lineLimit(1)
        }
    }

    private var positionRows: some View {
        ForEach(openPositions) { position in
            let pnl = position.unrealizedPnL ?? 0
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(position.instrumentID).font(.caption.monospaced())
                    Text(position.side.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                    Text("\(formatContracts(abs(position.quantity))) 张").font(.caption2.monospacedDigit())
                    Spacer()
                    Text(formatSigned(pnl)).font(.caption2.monospacedDigit()).foregroundStyle(pnl >= 0 ? .green : .red)
                }
                if let protection = protectionText(for: position) {
                    Text(protection).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            if startBlockedByKillSwitch {
                Text("账户风控已熔断，复位后才能启动").font(.caption2).foregroundStyle(.orange).lineLimit(1)
            }
            Spacer()
            Button(isRunning ? "暂停" : "启动") { model.toggleStrategy(config.id) }
                .buttonStyle(.bordered)
                .controlSize(.mini)
                .disabled(startBlockedByKillSwitch)
        }
    }

    /// Stop and take-profit of the entry that opened this position, relative
    /// to the exchange's average entry price.
    private func protectionText(for position: PositionSnapshot) -> String? {
        let order = model.orders.first {
            $0.strategyID == config.id && $0.instrumentID == position.instrumentID && !OrderLifecycle.isTerminal($0.status)
        }
        guard let signal = order?.signal, position.entryPrice > 0 else { return nil }
        var parts: [String] = []
        if let stop = signal.stopPrice {
            parts.append("止损 \(formatPrice(stop)) (\(formatSignedPercent(percentDelta(stop, from: position.entryPrice))))")
        }
        if let take = signal.takePrice {
            let label = (signal.takePrices?.count ?? 0) > 1 ? "止盈 TP1" : "止盈"
            parts.append("\(label) \(formatPrice(take)) (\(formatSignedPercent(percentDelta(take, from: position.entryPrice))))")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func percentDelta(_ price: Decimal, from entry: Decimal) -> Double {
        ((price - entry) / entry * 100).doubleValue
    }

    private func signalDirectionText(_ type: String) -> String {
        directionText(type) ?? type
    }

    private func directionText(_ rawValue: String) -> String? {
        switch rawValue.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "long", "buy", "entry_long", "做多": return "做多"
        case "short", "sell", "entry_short", "做空": return "做空"
        default: return nil
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
