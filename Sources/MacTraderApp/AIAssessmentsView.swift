import Foundation
import SwiftUI
import TradingDomain

private extension AIInstrumentAssessment {
    var directionLabel: String {
        switch direction {
        case .long: return "做多"
        case .short: return "做空"
        case .neutral: return "无明确方向"
        }
    }

    var directionColor: Color {
        switch direction {
        case .long: return .green
        case .short: return .red
        case .neutral: return .secondary
        }
    }

    var winRateLabel: String { winRate.map { String(format: "%.1f%%", $0 * 100) } ?? "未估计" }
    var riskRewardLabel: String { riskRewardRatio.map { String(format: "%.2f", $0) } ?? "未估计" }
    var eligibilityLabel: String { entryEligible ? "模型通过" : "需等待" }
}

/// Preserve the proposed price's digits, including very small token prices.
private func assessmentPrice(_ price: Decimal?) -> String {
    price.map { NSDecimalNumber(decimal: $0).stringValue } ?? "未确定"
}

private struct AIAssessmentReason: View {
    let assessment: AIInstrumentAssessment

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(assessment.reason)
            if !assessment.unmetConditions.isEmpty {
                Text("未满足：" + assessment.unmetConditions.joined(separator: "；"))
                    .foregroundStyle(.orange)
            }
            Text("方案置信度 \(String(format: "%.0f%%", assessment.confidence * 100))")
                .foregroundStyle(.secondary)
                .monospacedDigit()
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// Values captured from a selected log row stay fixed while newer decisions arrive.
struct AIAssessmentsSheet: View {
    let decision: AIDecision
    let decidedAt: Date?
    let audit: AIAuditRecord?
    @Environment(\.dismiss) private var dismiss
    @State private var selectedID: String?

    init(decision: AIDecision, decidedAt: Date? = nil, audit: AIAuditRecord? = nil) {
        self.decision = decision
        self.decidedAt = decidedAt
        self.audit = audit
    }

    private var assessments: [AIInstrumentAssessment] { decision.assessments }
    private var selectedAssessment: AIInstrumentAssessment? {
        assessments.first { $0.instrumentID == selectedID } ?? assessments.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("AI 逐币评估（\(assessments.count) 个合约）", systemImage: "list.bullet.rectangle")
                    .font(.title3.weight(.semibold))
                Spacer()
                if let date = decidedAt {
                    Text("评估时间 \(formatLocalTime(date))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Button("完成") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            Text("胜率是模型估计。建议挂单价、止损和止盈属于交易方案；实际挂单请查看账户挂单。每轮最多选择一个执行动作。")
                .font(.caption)
                .foregroundStyle(.secondary)

            Table(assessments, selection: $selectedID) {
                TableColumn("合约", value: \.instrumentID).width(min: 150, ideal: 160)
                TableColumn("多空方向") { row in
                    Text(row.directionLabel).foregroundStyle(row.directionColor)
                }.width(min: 65, ideal: 80)
                TableColumn("预计胜率") { row in Text(row.winRateLabel).monospacedDigit() }
                    .width(min: 65, ideal: 80)
                TableColumn("盈亏比") { row in Text(row.riskRewardLabel).monospacedDigit() }
                    .width(min: 60, ideal: 65)
                TableColumn("建议挂单价") { row in Text(assessmentPrice(row.limitPrice)).monospacedDigit() }
                    .width(min: 110, ideal: 130)
                TableColumn("止损价") { row in Text(assessmentPrice(row.stopLossPrice)).monospacedDigit() }
                    .width(min: 110, ideal: 130)
                TableColumn("止盈价") { row in Text(assessmentPrice(row.takeProfitPrice)).monospacedDigit() }
                    .width(min: 110, ideal: 130)
                TableColumn("模型条件") { row in Text(row.eligibilityLabel) }
                    .width(min: 70, ideal: 85)
            }
            .frame(minHeight: 270)

            if let selectedAssessment {
                VStack(alignment: .leading, spacing: 8) {
                    Text("\(selectedAssessment.instrumentID) · \(selectedAssessment.directionLabel)")
                        .font(.headline)
                    AIAssessmentReason(assessment: selectedAssessment)
                }
                .textSelection(.enabled)
                .font(.callout)
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
            }
            if audit?.accepted == false, let rawDecision = audit?.rawDecision, rawDecision.action != decision.action {
                Text("模型原始提案：\(rawDecision.action.rawValue)\(rawDecision.instrumentID.map { " · \($0)" } ?? "") · \(rawDecision.reason)")
                    .font(.caption)
                    .textSelection(.enabled)
            }
            if audit?.accepted == false, let reason = audit?.reason, reason != decision.reason {
                Text("服务端未通过：\(reason)").foregroundStyle(.orange).font(.caption)
            }
            Text("\(audit?.accepted == false ? "服务端未通过，本轮结果" : "本轮总决策")：\(decision.action.rawValue) · \(decision.reason)")
                .font(.caption)
                .foregroundStyle(audit?.accepted == false ? Color.orange : Color.secondary)
                .textSelection(.enabled)
        }
        .padding(20)
        .frame(width: 1080, height: 700)
    }
}
