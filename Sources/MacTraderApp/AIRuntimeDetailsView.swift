import Foundation
import SwiftUI
import TradingDomain

/// Decision history uses only its own audit values, never the latest status.
struct AIRuntimeDetailsView: View {
    @ObservedObject var model: DashboardModel
    let strategy: AIStrategyID
    @State private var selectedAssessment: AIRuntimeAssessmentSelection?

    init(model: DashboardModel, strategy: AIStrategyID = .codex) {
        self.model = model
        self.strategy = strategy
    }

    private var entries: [AIRuntimeLogEntry] {
        let records = model.aiDecisions(for: strategy).enumerated().sorted { lhs, rhs in
            let leftSecond = lhs.element.at?.timeIntervalSince1970.rounded(.down) ?? -.infinity
            let rightSecond = rhs.element.at?.timeIntervalSince1970.rounded(.down) ?? -.infinity
            if leftSecond == rightSecond { return lhs.offset > rhs.offset }
            return leftSecond > rightSecond
        }
        var seenDecisions = Set<String>()
        var result: [AIRuntimeLogEntry] = []
        for (index, record) in records {
            if record.type == "decision", let decision = record.decision {
                let identity = runtimeDecisionIdentity(decision)
                if seenDecisions.insert(identity).inserted {
                    result.append(AIRuntimeLogEntry(id: identity, record: record))
                }
            } else if record.type == "error", record.decision == nil,
                      record.rawDecision == nil,
                      record.snapshotId?.isEmpty != false {
                // Separate failures remain separate even when their text matches.
                let identity = "error:\(index):\(record.id)"
                result.append(AIRuntimeLogEntry(id: identity, record: record))
            }
        }
        return result
    }

    private var submissions: [String: LiveOrderResult] {
        var result: [String: LiveOrderResult] = [:]
        for record in model.aiAudit(for: strategy).reversed() {
            guard record.type == "submitted", let order = record.order,
                  let clientID = order.clientOrderID, result[clientID] == nil else { continue }
            result[clientID] = order
        }
        return result
    }

    var body: some View {
        let recordedSubmissions = submissions
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                if entries.isEmpty {
                    Text("尚无决策日志，评估完成后会在这里显示。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 20)
                }
                ForEach(entries) { entry in
                    HStack(alignment: .top, spacing: 14) {
                        Text(entry.record.at.map(runtimeTime) ?? "时间未记录")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .frame(width: 110, alignment: .leading)
                            .padding(.top, 2)
                        if let decision = entry.record.decision {
                            AIRuntimeDecisionRow(
                                decision: decision,
                                audit: entry.record,
                                submission: submission(for: entry, from: recordedSubmissions)
                            ) {
                                selectedAssessment = AIRuntimeAssessmentSelection(
                                    decision: decision,
                                    decidedAt: entry.record.at,
                                    audit: entry.record
                                )
                            }
                        } else {
                            AIRuntimeErrorRow(record: entry.record)
                        }
                    }
                    .padding(.vertical, 14)
                    Divider()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 4)
        }
        .sheet(item: $selectedAssessment) { selection in
            AIAssessmentsSheet(
                decision: selection.decision,
                decidedAt: selection.decidedAt,
                audit: selection.audit
            )
        }
    }

    private func submission(for entry: AIRuntimeLogEntry, from orders: [String: LiveOrderResult]) -> LiveOrderResult? {
        guard entry.record.accepted == true, let decision = entry.record.decision,
              decision.action == .open || decision.action == .close,
              let instrument = decision.instrumentID else { return nil }
        let clientID = String(("ai" + decision.decisionId.replacingOccurrences(of: "-", with: "")).prefix(32))
        guard let order = orders[clientID], order.instrumentID == instrument else { return nil }
        if let decidedAt = entry.record.at, order.submittedAt < decidedAt { return nil }
        return order
    }
}

private struct AIRuntimeLogEntry: Identifiable {
    let id: String
    let record: AIAuditRecord
}

private struct AIRuntimeAssessmentSelection: Identifiable {
    let decision: AIDecision
    let decidedAt: Date?
    let audit: AIAuditRecord

    var id: String { runtimeDecisionIdentity(decision) }
}

private struct AIRuntimeDecisionRow: View {
    let decision: AIDecision
    let audit: AIAuditRecord
    let submission: LiveOrderResult?
    let showAssessments: () -> Void

    private var isSystemHold: Bool { decision.reasonCode == "COORDINATOR_FAILED" }

    private var actionLabel: String {
        if isSystemHold { return "系统观望 · AI 汇总失败" }
        switch decision.action {
        case .hold: return "观望"
        case .open: return "开仓意图"
        case .close: return "平仓意图"
        case .cancel: return "撤单意图"
        }
    }

    private var instrument: String? {
        nonemptyRuntimeText(decision.instrumentID)
            ?? nonemptyRuntimeText(audit.rawDecision?.instrumentID)
            ?? nonemptyRuntimeText(audit.instrumentID)
    }

    private var reason: String {
        if let error = nonemptyRuntimeText(audit.error) { return error }
        if audit.accepted == false, let reason = nonemptyRuntimeText(audit.reason) { return reason }
        return nonemptyRuntimeText(decision.reason) ?? "原因未记录"
    }

    private var executionLabel: String {
        if let submission {
            if submission.status == "unknown" { return "委托结果待确认" }
            return "委托已提交"
        }
        if isSystemHold { return "未执行 · AI 汇总失败" }
        if audit.accepted == false { return "未执行 · 服务端校验未通过" }
        if decision.action == .hold { return "未执行 · 观望" }
        if audit.accepted == true { return "校验通过 · 暂无执行回执" }
        return "暂无执行回执"
    }

    private var executionColor: Color {
        isSystemHold || audit.accepted == false || audit.error != nil || submission?.status == "unknown" ? .orange : .secondary
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(actionLabel)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(isSystemHold ? Color.orange : Color.primary)
                if let instrument {
                    Text(instrument)
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                }
                Spacer(minLength: 0)
            }
            Text(reason)
                .font(.caption)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(executionLabel)
                    .foregroundStyle(executionColor)
                    .help(submission.map { "订单 \($0.orderID) · 返回状态 \($0.status)" }
                          ?? "校验通过只表示服务端接受该决策，提交情况以对应委托审计为准。")
                Spacer(minLength: 0)
            }
            .font(.caption)
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                if decision.assessments.isEmpty {
                    Text("逐币评估未记录")
                        .foregroundStyle(.secondary)
                } else {
                    Text("逐币评估 \(decision.assessments.count) 个 · 模型通过 \(decision.assessments.filter(\.entryEligible).count) 个")
                        .foregroundStyle(.secondary)
                        .help("模型通过表示模型认为符合开仓条件，委托是否提交以本条执行回执为准。")
                }
                Spacer(minLength: 8)
                Button("查看明细表", action: showAssessments)
                    .buttonStyle(.borderless)
                    .disabled(decision.assessments.isEmpty)
                    .help(decision.assessments.isEmpty ? "该历史决策未记录逐币评估，无法提供明细表。" : "查看这一轮的多空方向、预计胜率、盈亏比和建议挂单价。")
            }
            .font(.caption)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct AIRuntimeErrorRow: View {
    let record: AIAuditRecord

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("运行错误")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.orange)
            Text(nonemptyRuntimeText(record.error) ?? nonemptyRuntimeText(record.reason) ?? "错误详情未记录")
                .font(.caption)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private func runtimeDecisionIdentity(_ decision: AIDecision) -> String {
    "decision:\(decision.decisionId.count):\(decision.decisionId):\(decision.snapshotId)"
}

private func nonemptyRuntimeText(_ value: String?) -> String? {
    guard let text = value?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return nil }
    return text
}

private func runtimeTime(_ date: Date) -> String {
    date.formatted(.dateTime.month(.twoDigits).day(.twoDigits).hour(.twoDigits(amPM: .omitted)).minute().second().locale(Locale(identifier: "zh_CN")))
}
