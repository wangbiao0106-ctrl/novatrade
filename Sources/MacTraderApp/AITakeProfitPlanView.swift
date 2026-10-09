import Foundation
import SwiftUI
import TradingDomain

/// A model's staged exit proposal; execution is recorded separately in the audit.
struct AITakeProfitPlanView: View {
    let levels: [AITakeProfitLevel]
    var title = "分批止盈方案"

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).foregroundStyle(.secondary)
            ForEach(Array(levels.enumerated()), id: \.offset) { index, level in
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text("第 \(index + 1) 档")
                    Text(NSDecimalNumber(decimal: level.price).stringValue)
                        .monospacedDigit()
                    Text("比例 \(level.quantityPercent.formatted(.number.precision(.fractionLength(0...6))))%")
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
        }
        .font(.caption)
        .textSelection(.enabled)
    }
}
