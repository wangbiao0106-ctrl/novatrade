import Foundation
import SwiftUI
import TradingDomain
import TradingServiceClient

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
                AIControlModule(model: model)
                ForEach(model.strategies) { config in
                    StrategyStatusModule(config: config, status: model.strategyStatus(for: config), model: model)
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

struct AIControlModule: View {
    @ObservedObject var model: DashboardModel
    @State private var showingFlattenConfirmation = false
    @State private var showingAIActivity = false
    @State private var feedback: String?

    private var active: Bool { model.aiStatus.enabled && model.aiStatus.mode != .disabled && model.aiStatus.mode != .halted }
    private var statusDotColor: Color {
        if model.aiStatus.lastError != nil { return .red }
        if active { return .green }
        if model.aiStatus.mode == .halted { return .red }
        return .secondary
    }
    private var statusMessage: String {
        if model.aiStatus.lastError != nil { return "运行异常 · 详情见日志" }
        if model.aiStatus.mode == .halted { return "已暂停" }
        return active ? "运行中" : "已停用"
    }
    private var statusMessageColor: Color {
        model.aiStatus.lastError == nil ? .secondary : .red
    }
    private var modeLabel: String {
        switch model.aiStatus.mode {
        case .disabled: return "已关闭"
        case .shadow: return "观察"
        case .demoActive: return "模拟盘"
        case .liveArmed: return "实盘已授权"
        case .halted: return "已停止"
        }
    }
    private var latestActionLabel: String {
        guard let decision = model.aiStatus.lastDecision else { return "等待首次评估" }
        if decision.reasonCode == "COORDINATOR_FAILED" { return "系统观望" }
        switch decision.action {
        case .hold: return "观望"
        case .open: return "建议开仓"
        case .close: return "建议平仓"
        case .cancel: return "建议撤单"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Label("AI 策略", systemImage: "sparkles")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                Spacer()
                Text(modeLabel)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(model.aiStatus.mode == .liveArmed ? .orange : active ? .green : .secondary)
                Button { showingAIActivity = true } label: {
                    Image(systemName: "list.bullet.rectangle")
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .help("查看 AI 运行日志")
                .accessibilityLabel("AI 运行日志")
            }
            HStack(spacing: 8) {
                Circle()
                    .fill(statusDotColor)
                    .frame(width: 7, height: 7)
                Text(statusMessage)
                    .font(.caption2)
                    .foregroundStyle(statusMessageColor)
                    .lineLimit(2)
            }
            HStack {
                Text("最近：\(latestActionLabel)")
                    .foregroundStyle(.primary)
                Spacer(minLength: 4)
                if let date = model.aiStatus.lastDecisionAt {
                    Text(formatLocalTime(date)).foregroundStyle(.secondary)
                }
            }
            .font(.caption2)
            if let decision = model.aiStatus.lastDecision, let instrument = decision.instrumentID {
                Text(instrument)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            HStack(spacing: 8) {
                Button {
                    Task {
                        do {
                            if active { try await model.disableAI() } else { try await model.enableAI() }
                        } catch { feedback = error.localizedDescription }
                    }
                } label: {
                    Label(active ? "停用" : "启用", systemImage: active ? "pause.fill" : "play.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(model.isUpdatingAI || model.aiStatus.mode == .halted)

                Button { showingFlattenConfirmation = true } label: {
                    Label("平仓", systemImage: "xmark.octagon")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .tint(.red)
                .disabled(model.isUpdatingAI)
            }
            if let feedback {
                Text(feedback).font(.caption2).foregroundStyle(.red).lineLimit(2)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 10))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(active ? Color.green.opacity(0.34) : Color.white.opacity(0.09), lineWidth: 1)
        )
        .sheet(isPresented: $showingAIActivity) {
            AIActivitySheet(model: model)
        }
        .confirmationDialog("撤销挂单并平掉当前仓位？", isPresented: $showingFlattenConfirmation, titleVisibility: .visible) {
            Button("确认平仓", role: .destructive) {
                Task {
                    do { try await model.flattenAI() } catch { feedback = error.localizedDescription }
                }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("服务端将撤销待成交订单，并只使用 reduce-only 订单平仓。")
        }
    }
}

struct AISettingsSheet: View {
    @ObservedObject var model: DashboardModel
    @Environment(\.dismiss) private var dismiss
    @State private var config: AIConfig
    @State private var selectedInstrumentIDs: Set<String>
    @State private var instrumentSearch = ""
    @State private var feedback: String?

    init(model: DashboardModel) {
        self.model = model
        _config = State(initialValue: model.aiConfig)
        let configured = model.aiConfig.allowedInstruments
        _selectedInstrumentIDs = State(initialValue: Set(configured.isEmpty ? ["BTC-USDT-SWAP"] : configured))
    }

    /// The AI universe is a fixed allowlist selected by the user. Show the
    /// pending selection here so the settings preview updates immediately.
    private var observedInstrumentIDs: [String] {
        selectedInstrumentIDs.sorted()
    }

    private var observedInstrumentText: String {
        if observedInstrumentIDs.isEmpty {
            return "BTC-USDT-SWAP"
        }
        return observedInstrumentIDs.joined(separator: ", ")
    }

    private var filteredContracts: [PerpetualContract] {
        let query = instrumentSearch.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let sorted = model.contracts.sorted { $0.id < $1.id }
        guard !query.isEmpty else { return sorted }
        return sorted.filter { contract in
            [contract.id, contract.name, contract.shortName, contract.pairLabel]
                .contains { $0.lowercased().contains(query) }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("AI 策略设置", systemImage: "slider.horizontal.3")
                    .font(.title3.weight(.semibold))
                Spacer()
                Button("取消") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }

            Form {
                Section("运行与标的") {
                    Picker("运行模式", selection: $config.mode) {
                        Text("关闭").tag(AIRunMode.disabled)
                        Text("观察（只记录）").tag(AIRunMode.shadow)
                        Text("模拟盘自动下单").tag(AIRunMode.demoActive)
                        Text("实盘授权").tag(AIRunMode.liveArmed)
                    }
                    .frame(minWidth: 260)
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text("固定观察合约")
                            Spacer()
                            Text("已选 \(selectedInstrumentIDs.count) 个")
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                        TextField("搜索合约，例如 BTC-USDT-SWAP", text: $instrumentSearch)
                            .textFieldStyle(.roundedBorder)
                        if model.contracts.isEmpty {
                            Text("正在加载 USDT 线性永续合约…")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else {
                            ScrollView {
                                LazyVStack(alignment: .leading, spacing: 2) {
                                    ForEach(filteredContracts) { contract in
                                        let selected = selectedInstrumentIDs.contains(contract.id)
                                        Button {
                                            if selected {
                                                selectedInstrumentIDs.remove(contract.id)
                                            } else {
                                                selectedInstrumentIDs.insert(contract.id)
                                            }
                                        } label: {
                                            HStack(spacing: 8) {
                                                Image(systemName: selected ? "checkmark.square.fill" : "square")
                                                    .foregroundStyle(selected ? .mint : .secondary)
                                                VStack(alignment: .leading, spacing: 2) {
                                                    Text(contract.id)
                                                        .font(.caption.monospaced())
                                                    Text(contract.pairLabel)
                                                        .font(.caption2)
                                                        .foregroundStyle(.secondary)
                                                }
                                                Spacer()
                                            }
                                            .contentShape(Rectangle())
                                            .padding(.vertical, 4)
                                        }
                                        .buttonStyle(.plain)
                                    }
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .frame(minHeight: 120, maxHeight: 220)
                            .overlay {
                                if filteredContracts.isEmpty {
                                    Text("没有匹配的合约")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                        if selectedInstrumentIDs.isEmpty {
                            Text("至少选择一个合约；AI 不会自动补充未选合约。")
                                .font(.caption)
                                .foregroundStyle(.red)
                        }
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .firstTextBaseline) {
                            Text("当前观察标的")
                            Spacer(minLength: 12)
                            Text("固定合约池")
                                .foregroundStyle(.secondary)
                        }
                        Text(observedInstrumentText)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        Text("手动多选固定观察合约，可随时增删标的。")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Section("信号与频率") {
                    HStack {
                        Text("最低置信度").frame(width: 120, alignment: .leading)
                        Spacer()
                        TextField("0.65", value: $config.minimumConfidence, format: .number.precision(.fractionLength(2)))
                            .frame(width: 72)
                            .multilineTextAlignment(.trailing)
                    }
                    HStack {
                        Text("决策间隔（秒）").frame(width: 120, alignment: .leading)
                        Spacer()
                        TextField("30", value: $config.decisionIntervalSeconds, format: .number.precision(.fractionLength(1)))
                            .frame(width: 72)
                            .multilineTextAlignment(.trailing)
                    }
                    HStack {
                        Text("同标的冷却（秒）").frame(width: 120, alignment: .leading)
                        Spacer()
                        TextField("60", value: $config.cooldownSeconds, format: .number.precision(.fractionLength(1)))
                            .frame(width: 72)
                            .multilineTextAlignment(.trailing)
                    }
                }

                Section("交易限制") {
                    HStack {
                        Text("每日最多开仓单").frame(width: 150, alignment: .leading)
                        Spacer()
                        TextField("20", value: $config.maxDailyOrders, format: .number)
                            .frame(width: 72)
                            .multilineTextAlignment(.trailing)
                        Text("单").foregroundStyle(.secondary)
                    }
                    HStack {
                        Text("每日最多亏损单").frame(width: 150, alignment: .leading)
                        Spacer()
                        TextField("5", value: $config.maxDailyLosses, format: .number)
                            .frame(width: 72)
                            .multilineTextAlignment(.trailing)
                        Text("单").foregroundStyle(.secondary)
                    }
                    HStack {
                        Text("单笔保证金").frame(width: 150, alignment: .leading)
                        Spacer()
                        TextField("500", value: $config.marginPerOrderUSD, format: .number.precision(.fractionLength(2)))
                            .frame(width: 72)
                            .multilineTextAlignment(.trailing)
                        Text("USDT").foregroundStyle(.secondary)
                    }
                    HStack {
                        Text("最大杠杆").frame(width: 150, alignment: .leading)
                        Spacer()
                        TextField("5", value: $config.maxLeverage, format: .number.precision(.fractionLength(1)))
                            .frame(width: 72)
                            .multilineTextAlignment(.trailing)
                        Text("x").foregroundStyle(.secondary)
                    }
                    Text("每次开仓使用的杠杆由 AI 决定，但不会超过此上限。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("允许的动作") {
                    Toggle("允许开仓", isOn: $config.allowOpen)
                    Toggle("允许平仓", isOn: $config.allowClose)
                    Toggle("允许撤单", isOn: $config.allowCancel)
                    Toggle("开仓必须带止损", isOn: $config.requireStopLoss)
                }
            }
            .formStyle(.grouped)

            if let feedback {
                Text(feedback)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack {
                Spacer()
                Button("保存设置") { save() }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isUpdatingAI)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 540, height: 720)
    }

    private func save() {
        guard !selectedInstrumentIDs.isEmpty else {
            feedback = "至少选择一个固定观察合约。"
            return
        }
        let patch = AIPatch(
            enabled: config.mode == .disabled ? false : model.aiConfig.enabled,
            mode: config.mode,
            allowedInstruments: selectedInstrumentIDs.sorted(),
            minimumConfidence: config.minimumConfidence,
            decisionIntervalSeconds: config.decisionIntervalSeconds,
            cooldownSeconds: config.cooldownSeconds,
            allowOpen: config.allowOpen,
            allowClose: config.allowClose,
            allowCancel: config.allowCancel,
            requireStopLoss: config.requireStopLoss,
            maxDailyOrders: config.maxDailyOrders,
            maxDailyLosses: config.maxDailyLosses,
            marginPerOrderUSD: config.marginPerOrderUSD,
            maxLeverage: config.maxLeverage
        )
        Task {
            do {
                try await model.updateAI(patch)
                dismiss()
            } catch {
                feedback = error.localizedDescription
            }
        }
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

    private var isRunning: Bool { status?.state == .running || (status == nil && config.enabled) }
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
        guard let capital else {
            return AnyView(
                HStack(spacing: 6) {
                    Text("收益 --")
                        .font(.caption2.monospacedDigit().weight(.semibold))
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                }
            )
        }
        let realized = capital.realizedPnL
        let unrealized = capital.unrealizedPnL
        let total = realized + unrealized
        let totalColor: Color = {
            if total > 0 { return .green }
            if total < 0 { return .red }
            return .secondary
        }()
        return AnyView(VStack(alignment: .leading, spacing: 4) {
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
        })
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
