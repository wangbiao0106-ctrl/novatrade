import SwiftUI
import TradingDomain

struct NewStrategySheet: View {
    @ObservedObject var model: DashboardModel
    @Environment(\.dismiss) private var dismiss
    @State private var capitalPoolPercent = 100.0
    @State private var leverage = StrategyType.sweepReversalShort.defaultLeverage
    @State private var selectedRule: StrategyType = .sweepReversalShort
    @State private var step: NewStrategyStep = .choose
    @State private var isCreating = false
    @State private var creationErrorMessage: String?

    private enum NewStrategyStep {
        case choose
        case configure
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                if step == .configure {
                    Button {
                        step = .choose
                    } label: {
                        Image(systemName: "chevron.left")
                            .font(.headline)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("返回选择策略")
                    .help("返回选择策略")
                }
                Image(systemName: step == .choose ? "square.grid.3x3" : "slider.horizontal.3")
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(.mint)
                    .frame(width: 30, height: 30)
                    .background(Color.mint.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
                VStack(alignment: .leading, spacing: 2) {
                    Text(step == .choose ? "选择策略" : "配置策略")
                        .font(.title2.weight(.semibold))
                    if step == .configure {
                        Text("调整下单参数")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("选择一个策略开始配置")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }

            if step == .choose {
                strategySelection
            } else {
                configurationForm
            }

            Divider()
                .overlay(Color.white.opacity(0.08))
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Label(submissionTarget.text, systemImage: submissionTarget.icon)
                        .font(.caption)
                        .foregroundStyle(submissionTarget.color)
                    if step == .configure, model.riskSnapshot.killSwitch {
                        Label("账户风控已熔断，保存后仍不能启动；请在新日复位后启动", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                    }
                }
                Spacer()
                if step == .configure {
                    Button {
                        create()
                    } label: {
                        if isCreating {
                            ProgressView()
                                .controlSize(.small)
                            Text("保存中…")
                        } else {
                            Text("创建并保存")
                        }
                    }
                        .buttonStyle(.borderedProminent)
                        .tint(.mint)
                        .keyboardShortcut(.defaultAction)
                        .frame(minWidth: 104)
                        .disabled(!canCreate || isCreating)
                }
            }
        }
        .padding(22)
        .frame(width: 760, height: step == .choose ? 500 : 720)
        .preferredColorScheme(.dark)
        .onAppear {
            // Every tap on 添加 starts at the catalog, even if SwiftUI keeps
            // the sheet's state storage alive between presentations.
            step = .choose
            selectedRule = .sweepReversalShort
            leverage = selectedRule.defaultLeverage
            capitalPoolPercent = min(100, max(1, maxCapitalPoolPercent))
            isCreating = false
            creationErrorMessage = nil
        }
        .onChange(of: capitalPoolPercent) { _, value in
            let clamped = value.isFinite ? min(max(value, 1), max(1, maxCapitalPoolPercent)) : min(100, max(1, maxCapitalPoolPercent))
            if clamped != value {
                capitalPoolPercent = clamped
            }
        }
        .onChange(of: model.strategyCapitals) { _, _ in clampCapitalPoolPercent() }
        .onChange(of: model.accountOverview) { _, _ in clampCapitalPoolPercent() }
        .alert("创建策略失败", isPresented: Binding(
            get: { creationErrorMessage != nil },
            set: { if !$0 { creationErrorMessage = nil } }
        )) {
            Button("确定", role: .cancel) { creationErrorMessage = nil }
        } message: {
            Text(creationErrorMessage ?? "未知错误")
        }
    }

    private var strategySelection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("选择一个策略开始配置")
                .font(.headline)
            Text("卡片会告诉你它做什么、多久检查一次，以及如何下单。资金池只使用 USDT。")
                .font(.caption)
                .foregroundStyle(.secondary)

            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 12), count: 3), spacing: 12) {
                ForEach(StrategyType.availableCases, id: \.self) { strategyType in
                    Button {
                        selectedRule = strategyType
                        leverage = strategyType.defaultLeverage
                        step = .configure
                    } label: {
                        strategySelectionCard(strategyType)
                    }
                    .buttonStyle(.plain)
                    .disabled(model.hasStrategyType(strategyType))
                    .opacity(model.hasStrategyType(strategyType) ? 0.62 : 1)
                }
            }
            Spacer(minLength: 0)
        }
    }

    private func strategySelectionCard(_ strategyType: StrategyType) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: strategyIcon(strategyType))
                    .font(.title3)
                    .foregroundStyle(.mint)
                VStack(alignment: .leading, spacing: 3) {
                    Text(strategyType.displayName)
                        .font(.subheadline.weight(.semibold))
                        .multilineTextAlignment(.leading)
                    if model.hasStrategyType(strategyType) {
                        Text("已有实例 · 无法重复创建")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.orange)
                    }
                }
            }
            Text(strategySummary(strategyType))
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.leading)
                .lineLimit(3)
                .frame(maxWidth: .infinity, alignment: .leading)
            Divider().overlay(Color.white.opacity(0.08))
            HStack {
                Label(orderTypeDescription(strategyType), systemImage: orderTypeIcon(strategyType))
                Spacer()
                Text("默认 \(formatLeverage(strategyType.defaultLeverage)) 倍")
            }
            .font(.caption2.weight(.medium))
            .foregroundStyle(.secondary)
            Text("资金池：USDT · \(strategyType.signalCycleDescription)")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
        .padding(14)
        .frame(maxWidth: .infinity, minHeight: 188, alignment: .topLeading)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.09)))
        .contentShape(RoundedRectangle(cornerRadius: 10))
    }

    private var configurationForm: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 14) {
                configurationSection("策略配置", subtitle: "策略运行范围与信号规则") {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: strategyIcon(selectedRule))
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(.mint)
                            .frame(width: 34, height: 34)
                            .background(Color.mint.opacity(0.14), in: RoundedRectangle(cornerRadius: 9))
                        VStack(alignment: .leading, spacing: 5) {
                            Text(selectedRule.displayName)
                                .font(.headline.weight(.semibold))
                            Text(strategySummary(selectedRule))
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 0)
                        strategyTag(orderTypeDescription(selectedRule), systemImage: orderTypeIcon(selectedRule))
                    }
                    Divider().overlay(Color.white.opacity(0.08))
                    VStack(alignment: .leading, spacing: 8) {
                        infoRow("扫描范围", value: selectedRule.defaultScope.displayName)
                        infoRow("信号周期", value: selectedRule.signalCycleDescription)
                        infoRow("止损", value: selectedRule.stopLossDescription)
                        infoRow("止盈", value: selectedRule.takeProfitDescription)
                        infoRow("冷却", value: selectedRule.cooldownDescription)
                    }
                }

                configurationSection("资金与风控", subtitle: "资金池和杠杆可调；风控参数由策略固定，按资金池计算") {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(alignment: .firstTextBaseline) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("资金池比例")
                                    .font(.body.weight(.medium))
                                Text("仅使用 USDT")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            VStack(alignment: .trailing, spacing: 2) {
                                Text("\(String(format: "%.0f", capitalPoolPercent))%")
                                    .font(.title3.monospacedDigit().weight(.semibold))
                                    .foregroundStyle(.mint)
                                Text("可分配上限 \(String(format: "%.0f", maxCapitalPoolPercent))%")
                                    .font(.caption2.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                        }
                        HStack(spacing: 12) {
                            Slider(value: $capitalPoolPercent, in: capitalPoolRange, step: 1)
                                .tint(.mint)
                                .accessibilityLabel("策略资金池比例")
                                .accessibilityValue("\(String(format: "%.0f", capitalPoolPercent))%")
                            TextField("", value: $capitalPoolPercent, format: .number.precision(.fractionLength(0)))
                                .textFieldStyle(.roundedBorder)
                                .multilineTextAlignment(.trailing)
                                .frame(width: 58)
                                .accessibilityLabel("策略资金池比例")
                                .accessibilityValue("\(String(format: "%.0f", capitalPoolPercent))%")
                            Text("%")
                                .foregroundStyle(.secondary)
                        }
                        Divider().overlay(Color.white.opacity(0.08))
                        VStack(alignment: .leading, spacing: 8) {
                            infoRow("USDT 总资产", value: formatted(usdtTotalAssets), valueColor: .primary)
                            infoRow("已分配给其他策略", value: formatted(otherStrategyCapital))
                            infoRow("剩余可分配", value: formatted(remainingAllocatableCapital))
                            infoRow("本策略资金池", value: formatted(currentStrategyCapital), valueColor: .mint)
                        }
                        Divider().overlay(Color.white.opacity(0.08))
                        HStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("杠杆")
                                    .font(.body.weight(.medium))
                                Text("默认 \(formatLeverage(selectedRule.defaultLeverage)) 倍，只影响保证金占用")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                            Stepper(value: $leverage, in: selectedRule.leverageRange, step: 0.5) {
                                Text("\(formatLeverage(leverage)) 倍")
                                    .font(.body.monospacedDigit().weight(.semibold))
                                    .frame(minWidth: 58, alignment: .trailing)
                            }
                            .controlSize(.small)
                            .accessibilityLabel("杠杆倍数")
                            .accessibilityValue("\(formatLeverage(leverage)) 倍")
                            .accessibilityHint("使用加号或减号调整杠杆")
                        }
                        Divider().overlay(Color.white.opacity(0.08))
                        VStack(alignment: .leading, spacing: 8) {
                            infoRow("单笔风险", value: riskRowText(percent: effectiveRisk, amount: perTradeRiskAmount), valueColor: .primary)
                            infoRow("最多同时持仓", value: "\(maxConcurrentPositions) 个")
                            infoRow("开放风险上限", value: riskRowText(percent: selectedRule.maxOpenRiskPercent, amount: maxOpenRiskAmount))
                            infoRow("单笔最大名义", value: "不超过资金池可用余额，与杠杆无关")
                        }
                        if let perTradeRiskAmount, perTradeRiskAmount < Self.minimumUsefulRiskAmount {
                            Label("单笔风险额不足 \(formatUSD(Self.minimumUsefulRiskAmount))，多数合约会因最小张数无法下单，建议提高资金池比例", systemImage: "exclamationmark.triangle.fill")
                                .font(.caption2)
                                .foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            .padding(.bottom, 4)
        }
        .scrollIndicators(.automatic)
    }

    @ViewBuilder
    private func configurationSection<Content: View>(_ title: String, subtitle: String? = nil, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.headline.weight(.semibold))
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white.opacity(0.08)))
    }

    @ViewBuilder
    private func infoRow(_ title: String, value: String, valueColor: Color = .secondary) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(title)
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.callout)
                .foregroundStyle(valueColor)
                .multilineTextAlignment(.trailing)
                .fixedSize(horizontal: false, vertical: true)
                .layoutPriority(1)
        }
    }

    private func strategyTag(_ title: String, systemImage: String) -> some View {
        Label(title, systemImage: systemImage)
            .font(.caption.weight(.medium))
            .foregroundStyle(.mint)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(Color.mint.opacity(0.12), in: Capsule())
    }

    private var canCreate: Bool {
        !model.hasStrategyType(selectedRule) && usdtTotalAssets != nil && effectiveCapitalPoolPercent > 0
    }

    private func strategyIcon(_ strategyType: StrategyType) -> String {
        switch strategyType {
        case .sweepReversalShort: return "arrow.down.right.and.arrow.up.left"
        case .hlsr: return "waveform.path.ecg"
        case .doublePumpExhaustionShort: return "chart.bar.xaxis"
        case .external: return "questionmark"
        }
    }

    private func strategySummary(_ strategyType: StrategyType) -> String {
        switch strategyType {
        case .sweepReversalShort:
            return "找出冲高后第二次扫顶的币，确认转弱后做空。"
        case .hlsr:
            return "观察高位流动性扫顶，反转确认后分批做空并逐步止盈。"
        case .doublePumpExhaustionShort:
            return "观察滚动 24 小时翻倍后的 15 分钟冲高衰竭，确认收盘后做空。"
        case .external:
            return "策略包暂不可用。"
        }
    }

    private func orderTypeDescription(_ strategyType: StrategyType) -> String {
        switch strategyType {
        case .sweepReversalShort, .hlsr, .doublePumpExhaustionShort:
            return "市价下单"
        case .external:
            return "不可用"
        }
    }

    private func orderTypeIcon(_ strategyType: StrategyType) -> String {
        switch strategyType {
        case .sweepReversalShort, .hlsr, .doublePumpExhaustionShort: return "bolt.fill"
        case .external: return "questionmark"
        }
    }

    /// Risk is supplied by the selected strategy's laboratory defaults. It is
    /// shown in the risk summary but is intentionally not editable here.
    private var effectiveRisk: Double { min(selectedRule.defaultRiskPercent, selectedRule.maxRiskPercent) }

    private var usdtTotalAssets: Decimal? { model.accountOverview.usdtEquity }

    /// Below this the sized order is smaller than one contract on most
    /// instruments, so every signal would be refused at the lot-size check.
    private static let minimumUsefulRiskAmount: Decimal = 5

    private var maxConcurrentPositions: Int { Int(selectedRule.defaultParameters["maxConcurrentPositions"] ?? 1) }

    /// Stop-loss budget of one entry: a share of this strategy's own pool.
    private var perTradeRiskAmount: Decimal? {
        currentStrategyCapital.map { $0 * Decimal(effectiveRisk) / 100 }
    }

    private var maxOpenRiskAmount: Decimal? {
        currentStrategyCapital.map { $0 * Decimal(selectedRule.maxOpenRiskPercent) / 100 }
    }

    /// What the slider can still hand out, in USDT, after the other pools.
    private var remainingAllocatableCapital: Decimal? {
        guard usdtTotalAssets != nil else { return nil }
        return capitalAllocation.capital(for: capitalAllocation.availableAllocationPercent)
    }

    private func riskRowText(percent: Double, amount: Decimal?) -> String {
        "\(formatPercent(Decimal(percent))) 资金池 ≈ \(formatted(amount))"
    }

    /// Strategy entries are only ever sent to the OKX demo account; the
    /// footer says so in terms of the account that is actually connected.
    private var submissionTarget: (text: String, icon: String, color: Color) {
        switch model.accountOverview.mode {
        case .paper: return ("仅提交至当前 OKX 模拟账户", "shield.checkered", .secondary)
        case .live: return ("策略只向 OKX 模拟账户下单；当前连接的是实盘账户，策略不会提交订单", "exclamationmark.shield.fill", .orange)
        case .readOnly: return ("当前账户只读，策略不会提交订单", "lock.shield", .orange)
        }
    }

    private var capitalAllocation: StrategyCapitalAllocation {
        StrategyCapitalAllocation(totalCapital: usdtTotalAssets ?? 0, strategyCapitals: model.strategyCapitals)
    }

    private var otherStrategyCapital: Decimal? {
        usdtTotalAssets.map { _ in capitalAllocation.occupiedCapital }
    }

    private var maxCapitalPoolPercent: Double {
        guard usdtTotalAssets != nil else { return 100 }
        return capitalAllocation.availableAllocationPercent.doubleValue
    }

    private var effectiveCapitalPoolPercent: Double {
        let requested = Decimal(capitalPoolPercent)
        return capitalAllocation.effectiveAllocationPercent(for: requested).doubleValue
    }

    private var currentStrategyCapital: Decimal? {
        guard usdtTotalAssets != nil else { return nil }
        return capitalAllocation.capital(for: Decimal(effectiveCapitalPoolPercent))
    }

    private var capitalPoolRange: ClosedRange<Double> {
        1...max(1, maxCapitalPoolPercent)
    }

    private func clampCapitalPoolPercent() {
        let upperBound = max(1, maxCapitalPoolPercent)
        let clamped = min(max(capitalPoolPercent, 1), upperBound)
        if clamped != capitalPoolPercent { capitalPoolPercent = clamped }
    }

    private func formatted(_ value: Decimal?) -> String {
        value.map(formatUSD) ?? "--"
    }

    private func create() {
        guard !isCreating else { return }
        // 参数默认值来自领域层（与实验室 config 对齐），不再在界面里重复一份。
        var parameters = selectedRule.defaultParameters
        parameters["leverage"] = leverage
        let config = StrategyConfig(name: selectedRule.displayName, scope: selectedRule.defaultScope, interval: selectedRule.entryInterval, type: selectedRule, parameters: parameters, enabled: false, riskPercent: effectiveRisk, capitalPoolPercent: effectiveCapitalPoolPercent, cooldownBars: selectedRule.defaultCooldownBars)
        isCreating = true
        Task {
            defer { isCreating = false }
            do {
                try await model.createStrategy(config)
                dismiss()
            } catch {
                creationErrorMessage = error.localizedDescription
            }
        }
    }
}
